# Monitoraggio bandi e normative — Confindustria Catania

Sito in cui le imprese associate si registrano, scelgono settore, parole chiave e fonti, e consultano i bandi e le normative che le riguardano.

**Come funziona:** il sito è pubblicato gratis con **GitHub Pages**. Account, preferenze e novità sono gestiti da **Supabase**, che offre accesso con email e password e un database. Ogni impresa può leggere e modificare solo i propri dati: lo garantiscono le regole del database (Row Level Security), non il sito.

```
index.html            il sito (registrazione, accesso, pannello), con il logo già incorporato
config.js             indirizzo e chiave pubblica di Supabase (da compilare)
privacy.html          bozza dell'informativa privacy (da completare)
assets/               logo vettoriale (SVG) da usare anche altrove, e favicon
supabase/schema.sql   tabelle, regole di sicurezza e contenuti di esempio
```

Tempo necessario: circa 30 minuti. Non serve installare nulla sul computer.

---

## 1. Crea il database su Supabase

1. Vai su [supabase.com](https://supabase.com), crea un account e poi **New project**.
2. Come regione scegli **Central EU (Frankfurt)**, così i dati restano nell'Unione europea. Annota la password del database in un posto sicuro.
3. Quando il progetto è pronto apri **SQL Editor → New query**, incolla tutto il contenuto di `supabase/schema.sql` e premi **Run**. Deve comparire "Success".

## 2. Collega il sito al database

1. In Supabase apri **Project Settings**. Nella sezione **API** (o **Data API** / **API Keys**, a seconda della versione) copia:
   - il **Project URL**, es. `https://abcdefghijkl.supabase.co`
   - la chiave pubblica: **anon public** oppure **publishable** (inizia con `sb_publishable_`).
2. Apri `config.js` e incolla i due valori tra gli apici.

> Queste due informazioni sono pubbliche per natura e possono stare su GitHub. **Non inserire mai** la chiave `service_role` / `secret`: darebbe accesso completo a tutti i dati.

## 3. Pubblica il sito su GitHub Pages

1. Su [github.com](https://github.com) crea un nuovo repository, es. `monitoraggio-bandi`. Deve essere **Public**: GitHub Pages sui repository privati richiede un piano a pagamento.
2. Nel repository vuoto clicca **uploading an existing file** (oppure **Add file → Upload files**). Trascina **il contenuto** della cartella del progetto, non la cartella stessa, e premi **Commit changes**.
3. Vai in **Settings → Pages**. Alla voce **Build and deployment → Source** scegli **Deploy from a branch**, poi il ramo **main** e la cartella **/ (root)**, e premi **Save**.
4. Dopo 1–2 minuti il sito è online all'indirizzo `https://NOME-UTENTE.github.io/monitoraggio-bandi/`.

## 4. Configura l'accesso in Supabase

Questi passaggi sono necessari perché le email di conferma riportino le imprese al tuo sito.

1. Vai in **Authentication → URL Configuration**:
   - **Site URL**: l'indirizzo del sito, es. `https://NOME-UTENTE.github.io/monitoraggio-bandi/`
   - **Redirect URLs**: aggiungi lo stesso indirizzo.
2. Vai in **Authentication → Sign In / Providers → Email**:
   - lascia attivo **Confirm email**: ogni impresa conferma il proprio indirizzo prima di entrare;
   - imposta la lunghezza minima della password a **8**, come richiesto dal modulo.

## 5. Imposta l'invio delle email (indispensabile per registrare altre imprese)

Il servizio email incluso in Supabase serve solo per le prove. Invia pochi messaggi l'ora e, di norma, solo agli indirizzi dei membri del progetto. **Senza questo passaggio le imprese esterne non riceverebbero l'email di conferma.**

1. Procurati un servizio SMTP: la casella istituzionale dell'associazione oppure un servizio come Brevo, Mailgun, Amazon SES o Resend.
2. In Supabase apri le impostazioni **SMTP** della sezione Authentication (**Authentication → Emails → SMTP Settings**) e inserisci host, porta, utente, password e mittente (es. `bandi@…`).
3. Facoltativo ma consigliato: in **Authentication → Emails → Templates** traduci in italiano i messaggi di "Confirm signup" e "Reset password".

## 6. Prova

1. Apri il sito, registra un'impresa di prova e conferma l'email.
2. Accedi. Vedrai il pannello con i **contenuti di esempio**, segnalati da un avviso.
3. Prova "Password dimenticata?", poi aggiungi parole chiave e fonti ed esci e rientra: le preferenze restano salvate.

In Supabase, **Authentication → Users** mostra tutte le imprese registrate. In **Table Editor → profili** trovi le loro preferenze e la data del consenso.

---

## Prima di aprire il servizio alle imprese

- [ ] Completa `privacy.html` con il titolare e il DPO (le parti evidenziate sono da compilare).
- [ ] Cancella i contenuti di esempio: in SQL Editor esegui `delete from public.novita where esempio;`
- [ ] Attiva il monitoraggio reale (vedi sotto): senza, il pannello resta vuoto.
- [ ] Valuta il piano a pagamento di Supabase: i progetti gratuiti vengono **messi in pausa dopo una settimana senza attività** e non includono backup automatici giornalieri.
- [ ] Facoltativo: un dominio proprio, da impostare in **Settings → Pages → Custom domain** con un record CNAME presso il registrar.

## Il monitoraggio reale: cosa manca

Questo progetto copre sito, account e preferenze. La raccolta delle novità dalle fonti e l'invio del digest delle 7:00 sono un componente separato, da costruire come passo successivo. Il database è già predisposto:

- il motore di monitoraggio inserisce ogni bando o normativa nella tabella `novita` (tipo, titolo, fonte, url, data di pubblicazione, scadenza, settori);
- il sito mostra a ciascuna impresa solo le novità pertinenti, in base a fonti attive, parole chiave e settore;
- per il digest, il motore legge `profili` (frequenza, parole chiave, fonti) e invia l'email.

Una soluzione tipica è un'attività pianificata con **GitHub Actions** ogni mattina, oppure una **Edge Function** di Supabase con cron. Usa la chiave `service_role`, conservata **solo** nei *secrets* e mai nel codice del sito.

## Aggiornare il sito

Modifica i file e ricaricali su GitHub (**Add file → Upload files**, con gli stessi nomi): GitHub Pages pubblica la nuova versione in 1–2 minuti. Se cambi lo schema del database, riesegui `schema.sql`: è scritto per poter essere rieseguito senza errori.
