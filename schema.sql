-- =====================================================================
--  Monitoraggio bandi e normative — Confindustria Catania
--  Schema del database (Supabase / PostgreSQL)
--
--  Come usarlo: Supabase → SQL Editor → New query → incolla tutto → Run.
--  Si può rieseguire senza errori: crea solo ciò che manca.
-- =====================================================================


-- ---------------------------------------------------------------------
-- 1. PROFILI: un profilo per ogni impresa registrata
-- ---------------------------------------------------------------------
create table if not exists public.profili (
  id                  uuid primary key references auth.users (id) on delete cascade,
  azienda             text not null default '' check (char_length(azienda) <= 200),
  settore             text not null default 'Varie' check (char_length(settore) <= 80),
  parole_chiave       text[] not null default '{}'::text[] check (cardinality(parole_chiave) <= 30),
  fonti               jsonb not null default '[
                        {"nome": "Gazzetta Ufficiale", "attiva": true},
                        {"nome": "incentivi.gov.it", "attiva": true},
                        {"nome": "Bandi Regione Sicilia", "attiva": true},
                        {"nome": "Circolari Confindustria Catania", "attiva": true}
                      ]'::jsonb check (jsonb_array_length(fonti) <= 20),
  frequenza           text not null default 'giornaliera' check (frequenza in ('giornaliera', 'settimanale')),
  consenso_privacy_at timestamptz,
  creato_il           timestamptz not null default now()
);

alter table public.profili enable row level security;

drop policy if exists "profili: lettura del proprio" on public.profili;
create policy "profili: lettura del proprio" on public.profili
  for select to authenticated using ((select auth.uid()) = id);

drop policy if exists "profili: modifica del proprio" on public.profili;
create policy "profili: modifica del proprio" on public.profili
  for update to authenticated using ((select auth.uid()) = id) with check ((select auth.uid()) = id);

drop policy if exists "profili: creazione del proprio" on public.profili;
create policy "profili: creazione del proprio" on public.profili
  for insert to authenticated with check ((select auth.uid()) = id);

grant select, insert, update on public.profili to authenticated;


-- Crea automaticamente il profilo quando un'impresa si registra,
-- usando i dati inviati dal modulo (azienda, settore, consenso).
create or replace function public.crea_profilo_nuovo_utente()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profili (id, azienda, settore, consenso_privacy_at)
  values (
    new.id,
    left(coalesce(new.raw_user_meta_data ->> 'azienda', ''), 200),
    left(coalesce(nullif(new.raw_user_meta_data ->> 'settore', ''), 'Varie'), 80),
    case when (new.raw_user_meta_data ->> 'consenso') = 'true' then now() else null end
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.crea_profilo_nuovo_utente();


-- ---------------------------------------------------------------------
-- 2. NOVITÀ: bandi e normative raccolti dal monitoraggio
--    Leggibili da tutte le imprese registrate; scrivibili solo
--    dal motore di monitoraggio (chiave service_role), mai dal sito.
-- ---------------------------------------------------------------------
create table if not exists public.novita (
  id             bigint generated always as identity primary key,
  tipo           text not null check (tipo in ('bando', 'normativa')),
  titolo         text not null,
  sommario       text,
  fonte          text not null,
  url            text,
  pubblicato_il  date not null default current_date,
  scadenza       date,
  settori        text[] not null default '{}'::text[],   -- vuoto = interessa tutti
  esempio        boolean not null default false,          -- true = contenuto dimostrativo
  creato_il      timestamptz not null default now()
);

create index if not exists novita_pubblicato_idx on public.novita (pubblicato_il desc);

alter table public.novita enable row level security;

drop policy if exists "novita: lettura per utenti registrati" on public.novita;
create policy "novita: lettura per utenti registrati" on public.novita
  for select to authenticated using (true);

grant select on public.novita to authenticated;


-- ---------------------------------------------------------------------
-- 3. SALVATI: le novità che ogni impresa mette da parte
-- ---------------------------------------------------------------------
create table if not exists public.salvati (
  user_id    uuid not null references auth.users (id) on delete cascade,
  novita_id  bigint not null references public.novita (id) on delete cascade,
  salvato_il timestamptz not null default now(),
  primary key (user_id, novita_id)
);

alter table public.salvati enable row level security;

drop policy if exists "salvati: lettura dei propri" on public.salvati;
create policy "salvati: lettura dei propri" on public.salvati
  for select to authenticated using ((select auth.uid()) = user_id);

drop policy if exists "salvati: aggiunta dei propri" on public.salvati;
create policy "salvati: aggiunta dei propri" on public.salvati
  for insert to authenticated with check ((select auth.uid()) = user_id);

drop policy if exists "salvati: rimozione dei propri" on public.salvati;
create policy "salvati: rimozione dei propri" on public.salvati
  for delete to authenticated using ((select auth.uid()) = user_id);

grant select, insert, delete on public.salvati to authenticated;


-- ---------------------------------------------------------------------
-- 4. CONTENUTI DI ESEMPIO (solo per provare il sito)
--    Prima di andare in produzione:  delete from public.novita where esempio;
-- ---------------------------------------------------------------------
insert into public.novita (tipo, titolo, fonte, pubblicato_il, scadenza, settori, esempio)
select * from (values
  ('bando',     'Voucher digitalizzazione per le PMI siciliane',            'Bandi Regione Sicilia',          current_date,     current_date + 9,  array['Hi-Tech e ICT','Varie','Terziario Innovativo'], true),
  ('normativa', 'Decreto sul credito d''imposta per ricerca e sviluppo',    'Gazzetta Ufficiale',             current_date,     null::date,        '{}'::text[],                                     true),
  ('bando',     'Avviso FESR Sicilia: efficientamento energetico delle imprese', 'incentivi.gov.it',          current_date,     current_date + 23, array['Varie','Alimentari','Chimici e Chimico Farmaceutici'], true),
  ('normativa', 'Transizione 5.0: nuove FAQ sulle certificazioni',          'Circolari Confindustria Catania', current_date,    null::date,        '{}'::text[],                                     true),
  ('bando',     'Contributi per la sicurezza nei cantieri (edilizia)',      'incentivi.gov.it',               current_date - 1, current_date + 41, array['ANCE','Varie'],                          true),
  ('bando',     'Voucher per l''internazionalizzazione',                   'incentivi.gov.it',               current_date - 3, current_date + 56, '{}'::text[],                                     true),
  ('normativa', 'Codice appalti: correttivo e nuove soglie',                'Gazzetta Ufficiale',             current_date - 2, null::date,        array['ANCE','Consulenza'],                     true),
  ('bando',     'Contributi per riqualificare le strutture ricettive',      'Bandi Regione Sicilia',          current_date - 1, current_date + 30, array['Turismo, Cultura ed Eventi'],            true)
) as v(tipo, titolo, fonte, pubblicato_il, scadenza, settori, esempio)
where not exists (select 1 from public.novita where esempio);
