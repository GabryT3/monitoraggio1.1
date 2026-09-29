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


-- ---------------------------------------------------------------------
-- 6. PRESET PER SEZIONE MERCEOLOGICA
--    Parole chiave e fonti proposte a ogni impresa in base alla sezione.
--    Si applicano alla registrazione e quando l'impresa cambia sezione.
--    Si possono modificare da Supabase → Table Editor → preset_settori
--    (le righe già presenti non vengono sovrascritte rieseguendo questo file).
-- ---------------------------------------------------------------------
create table if not exists public.preset_settori (
  settore        text primary key,
  parole_chiave  text[] not null default '{}'::text[],
  fonti          jsonb  not null default '[]'::jsonb   -- [{"nome": "...", "url": "https://..."}]
);
alter table public.preset_settori enable row level security;   -- lettura solo tramite le funzioni

alter table public.fonti add column if not exists preimpostata boolean not null default false;

insert into public.preset_settori (settore, parole_chiave, fonti) values
  ('Acquedotti',
   array['servizio idrico', 'depurazione', 'perdite idriche', 'acque reflue', 'ARERA', 'ZES unica'],
   '[{"nome": "ARERA – Autorità di regolazione per energia, reti e ambiente", "url": "https://www.arera.it"},
     {"nome": "Utilitalia", "url": "https://www.utilitalia.it"}]'),
  ('Alimentari',
   array['agroalimentare', 'filiera', 'sicurezza alimentare', 'etichettatura', 'vitivinicolo', 'ZES unica'],
   '[{"nome": "Ministero dell''agricoltura (MASAF)", "url": "https://www.masaf.gov.it"},
     {"nome": "ISMEA", "url": "https://www.ismea.it"}]'),
  ('ANCE',
   array['appalti', 'codice dei contratti', 'edilizia', 'cantieri', 'revisione prezzi', 'bonus edilizi'],
   '[{"nome": "ANCE – Associazione nazionale costruttori edili", "url": "https://ance.it"},
     {"nome": "ANAC – Autorità nazionale anticorruzione", "url": "https://www.anticorruzione.it"}]'),
  ('Bancaria e Assicurativa',
   array['Fondo di garanzia', 'accesso al credito', 'antiriciclaggio', 'assicurazioni', 'confidi', 'ZES unica'],
   '[{"nome": "Banca d''Italia", "url": "https://www.bancaditalia.it"},
     {"nome": "Fondo di garanzia per le PMI", "url": "https://www.fondidigaranzia.it"}]'),
  ('Chimici e Chimico Farmaceutici',
   array['REACH', 'farmaceutica', 'sostanze chimiche', 'emissioni industriali', 'AIFA', 'ZES unica'],
   '[{"nome": "AIFA – Agenzia italiana del farmaco", "url": "https://www.aifa.gov.it"},
     {"nome": "Federchimica", "url": "https://www.federchimica.it"}]'),
  ('Consulenza',
   array['voucher', 'consulenza', 'manager dell''innovazione', 'crisi d''impresa', 'formazione', 'ZES unica'],
   '[{"nome": "Invitalia", "url": "https://www.invitalia.it"},
     {"nome": "Ministero delle imprese e del made in Italy", "url": "https://www.mimit.gov.it"}]'),
  ('Hi-Tech e ICT',
   array['digitalizzazione', 'intelligenza artificiale', 'cybersicurezza', 'transizione 5.0', 'cloud', 'ZES unica'],
   '[{"nome": "AgID – Agenzia per l''Italia digitale", "url": "https://www.agid.gov.it"},
     {"nome": "Agenzia per la cybersicurezza nazionale", "url": "https://www.acn.gov.it"}]'),
  ('Servizi Sanitari',
   array['sanità', 'accreditamento', 'strutture sanitarie', 'telemedicina', 'dispositivi medici', 'ZES unica'],
   '[{"nome": "Ministero della Salute", "url": "https://www.salute.gov.it"},
     {"nome": "AGENAS – Agenzia nazionale per i servizi sanitari regionali", "url": "https://www.agenas.gov.it"}]'),
  ('Terziario Innovativo',
   array['startup', 'ricerca e sviluppo', 'brevetti', 'trasferimento tecnologico', 'Smart&Start', 'ZES unica'],
   '[{"nome": "Invitalia", "url": "https://www.invitalia.it"},
     {"nome": "Ministero delle imprese e del made in Italy", "url": "https://www.mimit.gov.it"}]'),
  ('Trasporti e Concessionarie',
   array['autotrasporto', 'logistica', 'veicoli', 'portuale', 'gasolio', 'ZES unica'],
   '[{"nome": "Ministero delle infrastrutture e dei trasporti", "url": "https://www.mit.gov.it"},
     {"nome": "Autorità di sistema portuale del Mare di Sicilia orientale", "url": "https://www.adspmaresiciliaorientale.it"}]'),
  ('Turismo, Cultura ed Eventi',
   array['turismo', 'strutture ricettive', 'spettacolo', 'eventi', 'fiere', 'ZES unica'],
   '[{"nome": "Ministero del Turismo", "url": "https://www.ministeroturismo.gov.it"},
     {"nome": "Ministero della Cultura", "url": "https://cultura.gov.it"}]'),
  ('Varie',
   array['credito d''imposta', 'transizione 5.0', 'internazionalizzazione', 'beni strumentali', 'Mezzogiorno', 'ZES unica'],
   '[{"nome": "Ministero delle imprese e del made in Italy", "url": "https://www.mimit.gov.it"},
     {"nome": "Invitalia", "url": "https://www.invitalia.it"}]')
on conflict (settore) do nothing;

-- Fonti di interesse locale proposte a tutte le sezioni
create table if not exists public.preset_comuni (
  id    int generated always as identity primary key,
  nome  text not null,
  url   text not null unique
);
alter table public.preset_comuni enable row level security;
insert into public.preset_comuni (nome, url) values
  ('Camera di Commercio del Sud Est Sicilia', 'https://www.ctrgsr.camcom.gov.it'),
  ('IRFIS FinSicilia', 'https://www.irfis.it')
on conflict (url) do nothing;


-- Elenco delle fonti proposte per una sezione (comuni + specifiche), senza doppioni
create or replace function public.elenco_fonti_preset(p_settore text)
returns table (nome text, url text)
language sql
stable
security definer
set search_path = ''
as $$
  select distinct on (x.url) x.nome, x.url
  from (
    select c.nome, regexp_replace(btrim(c.url), '/+$', '') as url, 1 as ordine from public.preset_comuni c
    union all
    select left(e ->> 'nome', 120), regexp_replace(btrim(e ->> 'url'), '/+$', ''), 0
    from public.preset_settori p, jsonb_array_elements(p.fonti) e
    where p.settore = p_settore
  ) x
  where x.url ~* '^https?://'
  order by x.url, x.ordine;
$$;

-- Inserisce nel catalogo le fonti proposte per la sezione e le restituisce pronte per il profilo
create or replace function public.fonti_preset(p_settore text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  r     record;
  v_id  bigint;
  v_out jsonb := '[]'::jsonb;
begin
  for r in select * from public.elenco_fonti_preset(p_settore) loop
    insert into public.fonti as f (url, nome, preimpostata)
    values (r.url, r.nome, true)
    on conflict (url) do update set preimpostata = true
    returning f.id into v_id;
    v_out := v_out || jsonb_build_array(jsonb_build_object(
      'nome', left(r.nome, 80), 'url', r.url, 'fonte_id', v_id, 'attiva', true));
  end loop;
  return v_out;
end;
$$;

-- Tutte le fonti proposte nel catalogo, così il monitoraggio le controlla già prima delle registrazioni
create or replace function public.sincronizza_fonti_preset()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_n integer;
begin
  insert into public.fonti as f (url, nome, preimpostata)
  select distinct on (x.url) x.url, x.nome, true
  from (
    select regexp_replace(btrim(c.url), '/+$', '') as url, left(c.nome, 120) as nome from public.preset_comuni c
    union all
    select regexp_replace(btrim(e ->> 'url'), '/+$', ''), left(e ->> 'nome', 120)
    from public.preset_settori p, jsonb_array_elements(p.fonti) e
  ) x
  where x.url ~* '^https?://'
  on conflict (url) do update set preimpostata = true;
  get diagnostics v_n = row_count;
  return v_n;
end;
$$;

-- Anteprima per il modulo di registrazione (nessuna scrittura; disponibile anche prima dell'accesso)
create or replace function public.anteprima_settore(p_settore text)
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  select jsonb_build_object(
    'parole_chiave', coalesce((select to_jsonb(p.parole_chiave) from public.preset_settori p where p.settore = p_settore), '[]'::jsonb),
    'fonti', coalesce((select jsonb_agg(e.nome) from public.elenco_fonti_preset(p_settore) e), '[]'::jsonb));
$$;

-- Consigli per l'impresa che cambia sezione dal pannello
create or replace function public.consigli_settore(p_settore text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
begin
  if auth.uid() is null then
    raise exception 'Accesso richiesto' using errcode = '42501';
  end if;
  return jsonb_build_object(
    'parole_chiave', coalesce((select to_jsonb(p.parole_chiave) from public.preset_settori p where p.settore = p_settore), '[]'::jsonb),
    'fonti', public.fonti_preset(p_settore));
end;
$$;

revoke all on function public.elenco_fonti_preset(text) from public, anon, authenticated;
revoke all on function public.fonti_preset(text) from public, anon, authenticated;
revoke all on function public.sincronizza_fonti_preset() from public, anon, authenticated;
revoke all on function public.anteprima_settore(text) from public;
revoke all on function public.consigli_settore(text) from public, anon;
grant execute on function public.sincronizza_fonti_preset() to service_role;
grant execute on function public.anteprima_settore(text) to anon, authenticated;
grant execute on function public.consigli_settore(text) to authenticated;


-- Nuova versione della creazione del profilo: applica parole chiave e fonti della sezione.
-- Se qualcosa va storto con i preset, il profilo viene comunque creato con le impostazioni di base.
create or replace function public.crea_profilo_nuovo_utente()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_settore text := left(coalesce(nullif(new.raw_user_meta_data ->> 'settore', ''), 'Varie'), 80);
  v_base    jsonb := '[{"nome": "Gazzetta Ufficiale", "attiva": true},
                       {"nome": "incentivi.gov.it", "attiva": true},
                       {"nome": "Bandi Regione Sicilia", "attiva": true},
                       {"nome": "Circolari Confindustria Catania", "attiva": true}]'::jsonb;
  v_parole  text[] := '{}'::text[];
  v_fonti   jsonb := '[]'::jsonb;
begin
  begin
    select coalesce(p.parole_chiave, '{}'::text[]) into v_parole
    from public.preset_settori p where p.settore = v_settore;
    v_parole := coalesce(v_parole, '{}'::text[]);
    v_fonti := coalesce(public.fonti_preset(v_settore), '[]'::jsonb);
  exception when others then
    v_parole := '{}'::text[];
    v_fonti := '[]'::jsonb;
  end;

  insert into public.profili (id, azienda, settore, consenso_privacy_at, parole_chiave, fonti)
  values (
    new.id,
    left(coalesce(new.raw_user_meta_data ->> 'azienda', ''), 200),
    v_settore,
    case when (new.raw_user_meta_data ->> 'consenso') = 'true' then now() else null end,
    v_parole[1:30],
    v_base || v_fonti
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

-- Porta subito nel catalogo le fonti proposte, così vengono controllate entro un'ora
select public.sincronizza_fonti_preset();
