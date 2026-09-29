/*
 * Configurazione del collegamento a Supabase.
 *
 * Dove trovare i valori: Supabase → Project Settings → API (o "API Keys").
 *   SUPABASE_URL      → "Project URL", es. https://abcdefghijkl.supabase.co
 *   SUPABASE_ANON_KEY → la chiave pubblica: "anon public" oppure "publishable" (sb_publishable_...)
 *
 * Queste due informazioni possono stare in un repository pubblico: la sicurezza
 * dei dati è garantita dalle regole del database (Row Level Security).
 * NON inserire MAI qui la chiave "service_role" o "secret".
 */
window.APP_CONFIG = {
  SUPABASE_URL: 'https://hxmemnobljegxtubiwfh.supabase.co',
  SUPABASE_ANON_KEY: 'sb_publishable_o3Rps6BxQ79fhHVDnyutqQ_IlgCZmuZ'
};
