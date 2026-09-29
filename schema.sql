-- =====================================================================
--  Monitoraggio bandi e normative — Confindustria Catania
--  Schema del database (Supabase / PostgreSQL)
--
--  Come usarlo: Supabase → SQL Editor → New query → incolla tutto → Run.
--  Si può rieseguire senza errori: crea o aggiorna solo ciò che manca,
--  senza toccare imprese registrate, preferenze e novità salvate.
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
-- 2. NOVITÀ: bandi, normative e circolari raccolti dal monitoraggio
--    Leggibili da tutte le imprese registrate; scrivibili solo
--    dal motore di monitoraggio (chiave service_role), mai dal sito.
-- ---------------------------------------------------------------------
create table if not exists public.novita (
  id             bigint generated always as identity primary key,
  tipo           text not null check (tipo in ('bando', 'normativa', 'circolare', 'notizia')),
  titolo         text not null,
  sommario       text,
  fonte          text not null,
  url            text,
  pubblicato_il  date not null default current_date,
  scadenza       date,
  settori        text[] not null default '{}'::text[],   -- vuoto = interessa tutti
  esempio        boolean not null default false,          -- true = contenuto dimostrativo
  id_esterno     text,                                    -- identificativo nella fonte (evita i doppioni)
  creato_il      timestamptz not null default now()
);

-- Aggiornamento per chi aveva già creato la tabella con la versione precedente
alter table public.novita add column if not exists id_esterno text;
alter table public.novita drop constraint if exists novita_tipo_check;
alter table public.novita add constraint novita_tipo_check check (tipo in ('bando', 'normativa', 'circolare', 'notizia'));

create index if not exists novita_pubblicato_idx on public.novita (pubblicato_il desc);
create index if not exists novita_scadenza_idx on public.novita (scadenza);
create unique index if not exists novita_fonte_id_esterno_idx on public.novita (fonte, id_esterno);

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
-- 4. CONTENUTI DI ESEMPIO: non più necessari
--    Le novità reali arrivano dal monitoraggio automatico (cartella monitor/).
--    Qui si cancellano gli eventuali esempi rimasti dalla prima installazione.
-- ---------------------------------------------------------------------
delete from public.novita where esempio;


-- ---------------------------------------------------------------------
-- 5. FONTI AGGIUNTE DALLE IMPRESE
--    Catalogo comune: se più imprese aggiungono lo stesso sito, viene
--    controllato una volta sola. Il motore di monitoraggio (chiave
--    service_role) lo legge e aggiorna lo stato di ogni fonte.
--    Le imprese non leggono né scrivono direttamente la tabella: usano
--    le due funzioni qui sotto.
-- ---------------------------------------------------------------------
create table if not exists public.fonti (
  id                bigint generated always as identity primary key,
  url               text not null unique check (url ~* '^https?://' and char_length(url) <= 500),
  nome              text not null check (char_length(nome) between 1 and 120),
  tipo_rilevato     text check (tipo_rilevato in ('rss', 'pagina')),
  stato             text not null default 'in_attesa' check (stato in ('in_attesa', 'ok', 'errore')),
  messaggio         text,
  ultimo_controllo  timestamptz,
  voci_trovate      integer not null default 0,
  creato_da         uuid references auth.users (id) on delete set null,
  creato_il         timestamptz not null default now()
);

alter table public.fonti enable row level security;   -- nessuna policy: accesso solo tramite le funzioni

alter table public.novita add column if not exists fonte_id bigint references public.fonti (id) on delete cascade;
-- true = atto raccolto solo perché contiene la parola chiave di qualche impresa:
-- il sito lo mostra soltanto a chi segue una parola chiave corrispondente
alter table public.novita add column if not exists solo_parole boolean not null default false;
create index if not exists novita_fonte_id_idx on public.novita (fonte_id);


-- Aggiunge (o ritrova) una fonte nel catalogo e ne restituisce lo stato.
create or replace function public.aggiungi_fonte(p_url text, p_nome text default null)
returns table (id bigint, url text, nome text, stato text, tipo_rilevato text,
               messaggio text, ultimo_controllo timestamptz, voci_trovate integer)
language plpgsql
security definer
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_url     text;
  v_nome    text;
  v_host    text;
  v_recenti integer;
begin
  if auth.uid() is null then
    raise exception 'Accesso richiesto' using errcode = '42501';
  end if;

  -- normalizzazione dell'indirizzo: schema, niente frammento né barra finale, host in minuscolo
  v_url := btrim(coalesce(p_url, ''));
  if v_url !~* '^https?://' then
    v_url := 'https://' || v_url;
  end if;
  v_url := regexp_replace(v_url, '#.*$', '');
  v_url := regexp_replace(v_url, '/+$', '');
  v_url := lower(substring(v_url from '^(https?://[^/?#]+)'))
           || coalesce(substring(v_url from '^https?://[^/?#]+(.*)$'), '');
  if char_length(v_url) > 500
     or v_url !~* '^https?://[a-z0-9-]+(\.[a-z0-9-]+)+(:[0-9]+)?([/?].*)?$' then
    raise exception 'FONTE_URL_NON_VALIDO' using errcode = '22023';
  end if;

  v_host := substring(v_url from '^https?://(?:www\.)?([^/:?#]+)');
  v_nome := left(btrim(coalesce(nullif(btrim(p_nome), ''), v_host)), 120);
  if lower(v_nome) in ('gazzetta ufficiale', 'incentivi.gov.it', 'bandi regione sicilia', 'circolari confindustria catania') then
    v_nome := v_nome || ' (' || v_host || ')';
  end if;

  -- limite anti-abuso: al massimo 20 nuove fonti al giorno per impresa
  select count(*) into v_recenti
  from public.fonti f
  where f.creato_da = auth.uid() and f.creato_il > now() - interval '1 day';
  if v_recenti >= 20 then
    raise exception 'FONTE_TROPPE' using errcode = '54000';
  end if;

  insert into public.fonti as f (url, nome, creato_da)
  values (v_url, v_nome, auth.uid())
  on conflict (url) do nothing;

  return query
    select f.id, f.url, f.nome, f.stato, f.tipo_rilevato, f.messaggio, f.ultimo_controllo, f.voci_trovate
    from public.fonti f
    where f.url = v_url;
end;
$$;

-- Stato delle fonti seguite dall'impresa (solo quelle richieste, al massimo 50)
create or replace function public.stato_fonti(p_ids bigint[])
returns table (id bigint, url text, nome text, stato text, tipo_rilevato text,
               messaggio text, ultimo_controllo timestamptz, voci_trovate integer)
language sql
stable
security definer
set search_path = ''
as $$
  select f.id, f.url, f.nome, f.stato, f.tipo_rilevato, f.messaggio, f.ultimo_controllo, f.voci_trovate
  from public.fonti f
  where f.id = any (p_ids)
  limit 50;
$$;

revoke all on function public.aggiungi_fonte(text, text) from public, anon;
revoke all on function public.stato_fonti(bigint[]) from public, anon;
grant execute on function public.aggiungi_fonte(text, text) to authenticated;
grant execute on function public.stato_fonti(bigint[]) to authenticated;
