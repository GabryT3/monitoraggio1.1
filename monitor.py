#!/usr/bin/env python3
"""
Monitoraggio bandi e normative — Confindustria Catania
======================================================

Raccoglie ogni giorno le novità dalle fonti e le carica nella tabella
public.novita di Supabase, dove il sito le mostra alle imprese registrate.

Fonti (i nomi coincidono con quelli delle fonti nel pannello del sito):
  - "incentivi.gov.it"                 open data del MIMIT (licenza IODL 2.0)
  - "Gazzetta Ufficiale"               feed RSS della Serie Generale
  - "Bandi Regione Sicilia"            EuroInfoSicilia, bandi e avvisi aperti
  - "Circolari Confindustria Catania"  elenco pubblico delle circolari

Variabili d'ambiente:
  SUPABASE_URL           indirizzo del progetto, es. https://abcd.supabase.co
  SUPABASE_SERVICE_KEY   chiave "service_role" o "secret" (MAI nel sito, solo nei secrets di GitHub)
  PROVA=1                raccoglie e mostra i risultati senza scrivere nel database
  FONTI=gu,incentivi     (facoltativo) esegue solo alcune fonti: incentivi, gu, regione, circolari, personali
  SOLO_NUOVE=1           controlla soltanto le fonti aggiunte dalle imprese e non ancora verificate
                         (usato dal controllo orario, così una nuova fonte non aspetta il giorno dopo)

Oltre alle quattro fonti fisse, il programma controlla i siti aggiunti dalle imprese dal
pannello (tabella public.fonti): usa il feed RSS/Atom se il sito ne ha uno, altrimenti
segnala i nuovi link della pagina. Le parole chiave delle imprese servono anche a non
scartare gli atti della Gazzetta Ufficiale che le contengono.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import html
import io
import ipaddress
import json
import os
import re
import signal
import socket
import sys
import time
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROMA = ZoneInfo("Europe/Rome")
OGGI = datetime.now(ROMA).date()
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
      "MonitoraggioBandi-ConfindustriaCatania/1.1")
TIMEOUT = (15, 60)                                            # 15 s per collegarsi, 60 s tra un blocco di dati e l'altro
LIMITE_FONTE = int(os.environ.get("LIMITE_FONTE", "360"))     # al massimo 6 minuti per ciascuna fonte
LIMITE_SITO = int(os.environ.get("LIMITE_SITO", "60"))        # al massimo 1 minuto per ciascun sito aggiunto
LIMITE_SITI_TOTALE = int(os.environ.get("LIMITE_SITI_TOTALE", "900"))   # 15 minuti per tutti i siti; il resto alla volta dopo

FONTE_INCENTIVI = "incentivi.gov.it"
FONTE_GU = "Gazzetta Ufficiale"
FONTE_REGIONE = "Bandi Regione Sicilia"
FONTE_CIRCOLARI = "Circolari Confindustria Catania"

SETTORI = ["Acquedotti", "Alimentari", "ANCE", "Bancaria e Assicurativa", "Chimici e Chimico Farmaceutici",
           "Consulenza", "Hi-Tech e ICT", "Servizi Sanitari", "Terziario Innovativo", "Trasporti e Concessionarie",
           "Turismo, Cultura ed Eventi", "Varie"]


# ---------------------------------------------------------------------------
# Utilità
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


class TempoScaduto(BaseException):
    """Una fonte ha superato il tempo massimo (BaseException: non viene intercettata per sbaglio da 'except Exception')."""


@contextmanager
def limite_tempo(secondi: int, cosa: str):
    """Interrompe l'operazione se dura più di 'secondi' (solo dove il sistema lo consente, come su GitHub)."""
    if secondi <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def scaduto(_segnale, _frame):
        durata = f"{secondi // 60} minuti" if secondi >= 120 else f"{secondi} secondi"
        raise TempoScaduto(f"{cosa}: nessuna risposta completa entro {durata}; "
                           "il sito è lento o non risponde alle richieste automatiche")

    precedente = signal.signal(signal.SIGALRM, scaduto)
    signal.alarm(int(secondi))
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, precedente)


class SitoIrraggiungibile(RuntimeError):
    """Il server non risponde affatto (connessione rifiutata o scaduta)."""


def scarica(url: str, tentativi: int = 2) -> requests.Response:
    """GET con un nuovo tentativo in caso di errore temporaneo."""
    ultimo = None
    for i in range(tentativi):
        try:
            r = requests.get(url, timeout=TIMEOUT, headers={
                "User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,*/*;q=0.8"})
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            ultimo = e
            if i + 1 < tentativi:
                time.sleep(2)
        except Exception as e:  # noqa: BLE001
            ultimo = e
            if i + 1 < tentativi:
                time.sleep(2 * (i + 1))
    if isinstance(ultimo, (requests.ConnectionError, requests.Timeout)):
        raise SitoIrraggiungibile(f"{urlparse(url).netloc} non risponde ({type(ultimo).__name__})")
    raise RuntimeError(f"Download non riuscito: {url} ({ultimo})")


def pulisci(testo: str | None) -> str:
    """Toglie tag HTML, entità e spazi multipli."""
    if not testo:
        return ""
    t = BeautifulSoup(testo, "html.parser").get_text(" ") if "<" in testo else testo
    t = html.unescape(t).replace("\\,", ",")
    return re.sub(r"\s+", " ", t).strip()


def accorcia(testo: str, n: int) -> str:
    testo = pulisci(testo)
    if len(testo) <= n:
        return testo
    taglio = testo[:n].rsplit(" ", 1)[0]
    return taglio.rstrip(" ,;:.–-") + "…"


MESI = {"gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7,
        "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12}


def data_da_testo(s: str | None) -> date | None:
    """Riconosce 2026-09-26, 2026-09-26T10:00:00, 26/09/2026, 26.09.2026, 26 settembre 2026."""
    if not s:
        return None
    s = s.strip()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", s)
        if m:
            d, mo, y = map(int, m.groups())
        else:
            m = re.search(r"\b(\d{1,2})°?\s+(" + "|".join(MESI) + r")\s+(\d{4})\b", s, re.I)
            if not m:
                return None
            d, mo, y = int(m.group(1)), MESI[m.group(2).lower()], int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


# ---------------------------------------------------------------------------
# Classificazione per sezione merceologica
# (vuoto = la novità interessa tutte le imprese)
# ---------------------------------------------------------------------------
PAROLE_SETTORE = {
    "Acquedotti": [r"idric", r"acquedott", r"depurazion", r"acque reflue", r"servizio idrico"],
    "Alimentari": [r"agroaliment", r"alimentar", r"agricol", r"vitivinicol", r"\bpesca\b", r"ristorazion", r"zootecn"],
    "ANCE": [r"ediliz", r"\bedil", r"cantier", r"appalt", r"lavori pubblici", r"costruzion", r"contratti pubblici", r"opere pubbliche"],
    "Bancaria e Assicurativa": [r"\bbanc", r"assicura", r"fondo di garanzia", r"confidi", r"intermediari finanziari"],
    "Chimici e Chimico Farmaceutici": [r"chimic", r"farmaceut", r"\breach\b", r"biotecnolog"],
    "Consulenza": [r"consulen", r"professionist", r"manager dell", r"esperti valutatori"],
    "Hi-Tech e ICT": [r"digital", r"\bict\b", r"software", r"intelligenza artificiale", r"\bcyber", r"ultralarga",
                      r"quantum", r"tecnologie dell'informazione", r"deep.?tech", r"\b5g\b"],
    "Servizi Sanitari": [r"sanitar", r"ospedal", r"\bsanità\b", r"assistenza socio"],
    "Terziario Innovativo": [r"start.?up", r"innovativ", r"ricerca e sviluppo", r"trasferimento tecnologico", r"\bspin.?off"],
    "Trasporti e Concessionarie": [r"autotrasport", r"trasporto merci", r"trasporti", r"logistic", r"veicol",
                                   r"portual", r"gasolio", r"mobilità", r"concessionari"],
    "Turismo, Cultura ed Eventi": [r"turism", r"turistic", r"ricettiv", r"alberghi", r"\bcultura", r"culturali",
                                   r"spettacol", r"fieristic", r"\bfiere\b", r"eventi"],
}

# Mappa dei settori dell'open data di incentivi.gov.it verso le sezioni di Confindustria Catania
MAPPA_INCENTIVI = {
    "agroalimentare": ["Alimentari"], "ristorazione": ["Alimentari", "Turismo, Cultura ed Eventi"],
    "edilizia": ["ANCE"], "chimica e farmaceutica": ["Chimici e Chimico Farmaceutici"],
    "ict": ["Hi-Tech e ICT", "Terziario Innovativo"], "elettronica": ["Hi-Tech e ICT"],
    "salute": ["Servizi Sanitari"], "servizi di trasporto": ["Trasporti e Concessionarie"],
    "autoveicoli e altri mezzi di trasporto": ["Trasporti e Concessionarie"],
    "turismo": ["Turismo, Cultura ed Eventi"], "alberghiero": ["Turismo, Cultura ed Eventi"],
    "cultura": ["Turismo, Cultura ed Eventi"],
    "fornitura energia, acqua e gestione rifiuti": ["Acquedotti"],
    "altri servizi": ["Consulenza", "Terziario Innovativo"],
    "metallurgia": ["Varie"], "meccanica": ["Varie"], "moda e tessile": ["Varie"],
    "mobili, legno e carta": ["Varie"], "commercio": ["Varie"], "artigianato": ["Varie"],
}


def classifica(testo: str) -> list[str]:
    t = testo.lower()
    trovati = [s for s, pattern in PAROLE_SETTORE.items() if any(re.search(p, t) for p in pattern)]
    # Se un testo tocca molti settori insieme è di interesse generale
    return [] if len(trovati) > 4 else trovati


# ---------------------------------------------------------------------------
# Fonte 1: incentivi.gov.it (open data)
# ---------------------------------------------------------------------------
PAGINA_OPEN_DATA = "https://www.incentivi.gov.it/it/open-data"


def trova_file_open_data() -> list[str]:
    candidati: list[str] = []
    log("  leggo la pagina degli open data…")
    try:
        pagina = scarica(PAGINA_OPEN_DATA).text
        link = re.findall(r"""(?:href|src)=["']([^"']*open-data/[^"']+\.(?:json|zip|gz|csv))["']""", pagina, re.I)
        link = [urljoin(PAGINA_OPEN_DATA, l) for l in link]
        # preferenza: JSON, poi CSV compresso, poi CSV
        ordine = lambda u: (0 if u.lower().endswith(".json") else 1 if u.lower().endswith((".zip", ".gz")) else 2)
        candidati += sorted(dict.fromkeys(link), key=ordine)
        log(f"  trovati {len(candidati)} file di dati nella pagina")
    except SitoIrraggiungibile:
        raise   # il sito non risponde: inutile provare altri indirizzi dello stesso sito
    except Exception as e:  # noqa: BLE001
        log(f"  pagina open data non leggibile ({e}); provo gli indirizzi degli ultimi giorni")
    if candidati:
        return candidati
    # Ripiego: il file ha un nome con la data, es. 2025-4-5_opendata-export.csv
    for i in range(0, 8):
        d = OGGI - timedelta(days=i)
        for nome in (f"{d.year}-{d.month}-{d.day}_opendata-export", f"{d:%Y-%m-%d}_opendata-export"):
            for est in ("json", "csv"):
                candidati.append(f"https://www.incentivi.gov.it/sites/default/files/open-data/{nome}.{est}")
    return list(dict.fromkeys(candidati))


def leggi_open_data(contenuto: bytes, url: str) -> list[dict]:
    nome = urlparse(url).path.lower()
    if nome.endswith(".zip") or contenuto[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(contenuto)) as z:
            interno = next((n for n in z.namelist() if n.lower().endswith((".json", ".csv"))), None)
            if not interno:
                raise ValueError("archivio zip senza CSV/JSON")
            return leggi_open_data(z.read(interno), interno)
    if nome.endswith(".gz") or contenuto[:2] == b"\x1f\x8b":
        return leggi_open_data(gzip.decompress(contenuto), nome[:-3])
    testo = contenuto.decode("utf-8-sig", errors="replace")
    if testo.lstrip().startswith(("[", "{")):
        dati = json.loads(testo)
        if isinstance(dati, dict):
            dati = next((v for v in dati.values() if isinstance(v, list)), [])
        return [dict(r) for r in dati if isinstance(r, dict)]
    lettore = csv.DictReader(io.StringIO(testo), escapechar="\\", quotechar='"')
    return [dict(r) for r in lettore]


def campo(riga: dict, *nomi: str) -> str:
    """Legge un campo ignorando maiuscole/minuscole e differenze tra '_' e spazi."""
    norm = {re.sub(r"[\s_]+", "_", str(k)).lower(): v for k, v in riga.items()}
    for n in nomi:
        v = norm.get(re.sub(r"[\s_]+", "_", n).lower())
        if v not in (None, ""):
            return str(v) if not isinstance(v, list) else ",".join(map(str, v))
    return ""


def elenco(valore: str) -> list[str]:
    """Separa un elenco: la virgola senza spazio divide le voci, quella seguita da spazio fa parte del nome
    (es. "Agroalimentare,Mobili, Legno e Carta" → ["Agroalimentare", "Mobili, Legno e Carta"])."""
    if not valore:
        return []
    valore = valore.replace("\\,", ", ")
    return [p.strip() for p in re.split(r",(?=\S)|;", valore) if p.strip()]


def fonte_incentivi() -> list[dict]:
    righe, usato = None, None
    for url in trova_file_open_data():
        try:
            log(f"  scarico {url.rsplit('/', 1)[-1]}…")
            t0 = time.monotonic()
            r = scarica(url, tentativi=1 if "_opendata-export" in url else 2)
            log(f"  scaricati {len(r.content) / 1_048_576:.1f} MB in {time.monotonic() - t0:.0f} s, lettura dei dati…")
            righe = leggi_open_data(r.content, url)
            if righe:
                usato = url
                break
        except SitoIrraggiungibile:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"    non utilizzabile: {str(e)[:120]}")
            continue
    if not righe:
        raise RuntimeError("nessun file open data valido trovato su incentivi.gov.it")
    log(f"  file: {usato} ({len(righe)} incentivi)")

    risultati = []
    for r in righe:
        idx = campo(r, "ID_Incentivo", "id")
        titolo = pulisci(campo(r, "Titolo", "title"))
        if not idx or not titolo:
            continue
        regioni = campo(r, "Regioni").lower()
        ambito = campo(r, "Ambito_territoriale").lower()
        if regioni and "sicilia" not in regioni and "nazional" not in ambito:
            continue
        soggetti = campo(r, "Tipologia_Soggetto").lower()
        if soggetti and "impresa" not in soggetti:
            continue
        apertura = data_da_testo(campo(r, "Data_apertura"))
        chiusura = data_da_testo(campo(r, "Data_chiusura"))
        if chiusura and chiusura < OGGI:
            continue
        if apertura and apertura > OGGI + timedelta(days=90):
            continue
        aggiornamento = data_da_testo(campo(r, "Data_ultimo_aggiornamento"))
        pubblicato = min(apertura or aggiornamento or OGGI, OGGI)

        settori_fonte = [s.strip().lower() for s in elenco(campo(r, "Settore_Attivita"))]
        tutti = (not settori_fonte or len(settori_fonte) >= 10
                 or any("tutti" in s for s in settori_fonte)
                 or "tutti" in campo(r, "Codici_ATECO").lower())
        settori: list[str] = []
        if not tutti:
            for s in settori_fonte:
                settori += MAPPA_INCENTIVI.get(s, [])
            settori = sorted(set(settori)) if len(set(settori)) <= 4 else []

        descr = re.sub(r"^\s*Cos['’]è\s*", "", pulisci(campo(r, "Descrizione")))
        forma = ", ".join(elenco(pulisci(campo(r, "Forma_agevolazione"))))
        sommario = accorcia(descr, 280)
        if forma:
            sommario = (f"{forma}. " + sommario).strip()
        link = campo(r, "Link_istituzionale")
        if not re.match(r"^https?://", link or ""):
            link = "https://www.incentivi.gov.it/it/catalogo"
        risultati.append({
            "tipo": "bando", "titolo": accorcia(titolo, 300), "sommario": accorcia(sommario, 400),
            "fonte": FONTE_INCENTIVI, "url": link, "pubblicato_il": iso(pubblicato),
            "scadenza": iso(chiusura), "settori": settori, "id_esterno": f"inc-{idx}",
            "attivo": True,
        })
    return risultati


# ---------------------------------------------------------------------------
# Fonte 2: Gazzetta Ufficiale — Serie Generale (feed RSS)
# ---------------------------------------------------------------------------
FEED_GU = "https://www.gazzettaufficiale.it/rss/SG"

GU_ESCLUDI = re.compile("|".join([
    r"medicinal", r"immissione in commercio", r"assegno straordinario vitalizio", r"parrocchi", r"exequatur",
    r"onorificenz", r"medaglia", r"comizi", r"elezion", r"scioglimento del consiglio", r"liquidazione coatta",
    r"salesian", r"personalit[aà] giuridica", r"diocesi", r"confraternit", r"cittadinanza", r"\bstemma\b",
    r"sospensione del sindaco", r"gonfalone", r"cambio (?:del )?cognome", r"dispositivi medici",
    r"procedura di amministrazione straordinaria", r"sostituzione del commissario liquidatore",
]), re.I)

GU_INCLUDI = re.compile("|".join([
    r"impres", r"\bpmi\b", r"credito d'imposta", r"incentiv", r"agevolaz", r"contribut", r"finanziament",
    r"\bfondo\b", r"investiment", r"lavor", r"occupazion", r"assunzion", r"contratt[oi] collettiv",
    r"costo (?:medio )?orario", r"fiscal", r"tribut", r"\biva\b", r"accise", r"impost", r"energ", r"elettric",
    r"\bgas\b", r"carburant", r"gasolio", r"rifiut", r"ambient", r"emission", r"appalt", r"contratti pubblici",
    r"export", r"internazionalizz", r"\bzes\b", r"mezzogiorno", r"sicili", r"digital", r"innovazion",
    r"ricerca e sviluppo", r"transizione", r"filier", r"autotrasport", r"turism", r"commerci", r"industri",
    r"brevett", r"marchi", r"made in italy", r"sicurezza sul lavoro", r"\binail\b", r"\binps\b", r"previdenz",
]), re.I)

GU_ATTI_PRINCIPALI = re.compile(r"^(?:LEGGE|DECRETO-LEGGE|DECRETO LEGISLATIVO)\b", re.I)

# Parole chiave seguite dalle imprese (caricate dal database prima della raccolta)
PAROLE_IMPRESE: re.Pattern | None = None


def imposta_parole_imprese(parole: list[str]) -> None:
    global PAROLE_IMPRESE
    pulite = sorted({p.strip().lower() for p in parole if p and len(p.strip()) >= 3})
    # apostrofo dritto e tipografico sono equivalenti; spazi multipli ammessi
    schema = lambda p: "".join("['’]" if c in "'’" else r"\s+" if c == " " else re.escape(c) for c in p)
    PAROLE_IMPRESE = re.compile("|".join(schema(p) for p in pulite), re.I) if pulite else None


def fonte_gu() -> list[dict]:
    xml = scarica(FEED_GU).content
    radice = ET.fromstring(xml)
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    risultati, scartati = [], 0
    for item in radice.iter("item"):
        intestazione = pulisci(item.findtext("title"))
        link = (item.findtext("link") or "").strip().replace("http://", "https://", 1)
        testo = pulisci(item.findtext("content:encoded", namespaces=ns) or item.findtext("description"))
        codice = re.search(r"\((\d{2}[A-Z]\d{4,6})\)\s*$", testo)
        testo_pulito = re.sub(r"\s*\(\d{2}[A-Z]\d{4,6}\)\s*$", "", testo).strip().rstrip(".")
        tutto = f"{intestazione} {testo_pulito}"
        generale = not GU_ESCLUDI.search(tutto) and bool(GU_ATTI_PRINCIPALI.search(intestazione) or GU_INCLUDI.search(tutto))
        per_parola = bool(PAROLE_IMPRESE and PAROLE_IMPRESE.search(tutto))
        if not generale and not per_parola:
            scartati += 1
            continue
        try:
            pub = parsedate_to_datetime(item.findtext("pubDate")).astimezone(ROMA).date()
        except Exception:  # noqa: BLE001
            pub = OGGI
        ident = codice.group(1) if codice else (link.rstrip("/").split("/")[-2] if "/eli/" in link else link)
        risultati.append({
            "tipo": "normativa", "titolo": accorcia(testo_pulito or intestazione, 300),
            "sommario": accorcia(intestazione, 200), "fonte": FONTE_GU, "url": link or FEED_GU,
            "pubblicato_il": iso(pub), "scadenza": None, "settori": classifica(tutto),
            "id_esterno": f"gu-{ident}",
            # raccolto solo perché contiene la parola chiave di un'impresa: lo vede solo chi la segue
            "solo_parole": not generale,
        })
    per_parole = sum(v["solo_parole"] for v in risultati)
    log(f"  {len(risultati)} atti pertinenti ({per_parole} per le parole chiave delle imprese), "
        f"{scartati} scartati perché non riguardano le imprese")
    return risultati


# ---------------------------------------------------------------------------
# Fonte 3: Regione Siciliana — EuroInfoSicilia, bandi e avvisi aperti
# ---------------------------------------------------------------------------
REGIONE_FEED = "https://www.euroinfosicilia.it/category/bandi/bandi-e-avvisi-aperti/feed/"
REGIONE_PAGINA = "https://www.euroinfosicilia.it/bandi-e-avvisi-aperti/?sezione=aperti"
REGIONE_ESCLUDI = re.compile(r"letter[ae] d['’]invito|\binviti\b.*\bcomun|inviti ai comuni|riapertura termini inviti", re.I)


def scadenza_da_testo(testo: str) -> date | None:
    """Cerca una data di scadenza esplicita ("scadenza ... 30/11/2026", "entro il 30 novembre 2026", "fino al ...")."""
    for m in re.finditer(r"(?:scadenz\w*|entro (?:e non oltre )?(?:il|le ore [\d.:]+ del)|fino al|termine ultimo[^.]{0,20}?)\s*[:\-]?\s*"
                         r"((?:\d{1,2}[/.]\d{1,2}[/.]\d{4})|(?:\d{1,2}°?\s+[a-z]+\s+\d{4}))", testo, re.I):
        d = data_da_testo(m.group(1))
        if d and d >= OGGI - timedelta(days=1):
            return d
    return None


def fonte_regione() -> list[dict]:
    voci = []
    try:
        radice = ET.fromstring(scarica(REGIONE_FEED).content)
        for item in radice.iter("item"):
            titolo = pulisci(item.findtext("title"))
            link = (item.findtext("link") or "").strip()
            descr = pulisci(item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or item.findtext("description"))
            try:
                pub = parsedate_to_datetime(item.findtext("pubDate")).astimezone(ROMA).date()
            except Exception:  # noqa: BLE001
                pub = OGGI
            voci.append((titolo, link, descr, pub))
        log(f"  feed RSS: {len(voci)} voci")
    except Exception as e:  # noqa: BLE001
        log(f"  feed RSS non disponibile ({e}); leggo la pagina dei bandi aperti")
        soup = BeautifulSoup(scarica(REGIONE_PAGINA).text, "html.parser")
        for h in soup.find_all(["h2", "h3"]):
            a = h.find("a", href=True)
            if not a or "euroinfosicilia.it" not in urljoin(REGIONE_PAGINA, a["href"]):
                continue
            blocco = []
            for fratello in h.find_all_next(string=True, limit=40):
                titolo_padre = fratello.find_parent(["h2", "h3"])
                if titolo_padre is h:
                    continue
                if titolo_padre is not None:
                    break
                if fratello.find_parent("a") is None:  # esclude le etichette di categoria
                    blocco.append(str(fratello))
            testo = pulisci(" ".join(blocco))
            m = re.search(r"Data pubblicazione:\s*(\d{1,2}\s+\w+\s+\d{4})", testo)
            pub = data_da_testo(m.group(1)) if m else OGGI
            testo = re.sub(r"Data pubblicazione:\s*\d{1,2}\s+\w+\s+\d{4}", "", testo).strip()
            voci.append((pulisci(a.get_text()), urljoin(REGIONE_PAGINA, a["href"]), testo, pub or OGGI))
        log(f"  pagina: {len(voci)} voci")

    risultati = []
    for titolo, link, descr, pub in voci:
        if not titolo or not link or REGIONE_ESCLUDI.search(titolo):
            continue
        if pub < OGGI - timedelta(days=365):
            continue
        tutto = f"{titolo} {descr}"
        risultati.append({
            "tipo": "bando", "titolo": accorcia(titolo, 300), "sommario": accorcia(descr, 400),
            "fonte": FONTE_REGIONE, "url": link, "pubblicato_il": iso(min(pub, OGGI)),
            "scadenza": iso(scadenza_da_testo(descr)), "settori": classifica(tutto),
            "id_esterno": "ris-" + urlparse(link).path.strip("/").split("/")[-1][:180],
        })
    return risultati


# ---------------------------------------------------------------------------
# Fonte 4: Circolari Confindustria Catania (elenco pubblico; testo per i soci)
# ---------------------------------------------------------------------------
CIRCOLARI_PAGINA = "https://www.confindustriact.it/circolari/"


def fonte_circolari() -> list[dict]:
    soup = BeautifulSoup(scarica(CIRCOLARI_PAGINA).text, "html.parser")
    risultati, visti = [], set()
    tematica = ""
    for a in soup.find_all("a", href=True):
        href = urljoin(CIRCOLARI_PAGINA, a["href"])
        if "/tematiche/" in href:
            tematica = pulisci(a.get_text())
            continue
        if "/circolari_cfct/" not in href or href in visti:
            continue
        titolo = pulisci(a.get_text()).strip(" –-")
        if len(titolo) < 5:
            continue
        visti.add(href)
        dopo = []
        for s in a.find_all_next(string=True, limit=25):
            p = s.find_parent("a", href=True)
            if p is not None and p is not a and ("/circolari_cfct/" in p["href"] or "/tematiche/" in p["href"]):
                break
            dopo.append(str(s))
        testo = pulisci(" ".join(dopo))
        num = re.search(r"Circolare\s*n\.?\s*°?\s*(\d+)", testo, re.I)
        pub = data_da_testo(testo) or OGGI
        sommario = " · ".join(x for x in [f"Circolare n.{num.group(1)}" if num else "", tematica] if x)
        risultati.append({
            "tipo": "circolare", "titolo": accorcia(titolo, 300), "sommario": sommario or None,
            "fonte": FONTE_CIRCOLARI, "url": href, "pubblicato_il": iso(min(pub, OGGI)),
            "scadenza": None, "settori": [],  # le circolari sono rivolte a tutti gli associati
            "id_esterno": "cfct-" + urlparse(href).path.strip("/").split("/")[-1][:180],
        })
    return risultati


# ---------------------------------------------------------------------------
# Fonte 5: siti aggiunti dalle imprese dal pannello
# ---------------------------------------------------------------------------
MAX_BYTE = 5 * 1024 * 1024
MAX_VOCI_FONTE = 40
TIPO_BANDO = re.compile(r"\bband[oi]\b|\bavvis[oi]\b|contribut|finanziament|agevolaz|voucher|incentiv|\bcall\b|manifestazion[ei] di interesse", re.I)
TIPO_NORMA = re.compile(r"decret|\blegge\b|regolament|normativ|d\.\s?lgs|ordinanz|delibera|circolare|direttiva", re.I)


def indirizzo_consentito(url: str) -> bool:
    """Blocca indirizzi locali o privati (protezione da usi impropri)."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def scarica_sicuro(url: str) -> requests.Response:
    if not indirizzo_consentito(url):
        raise RuntimeError("indirizzo non consentito o inesistente")
    r = scarica(url, tentativi=2)
    if not indirizzo_consentito(r.url):
        raise RuntimeError("il sito reindirizza a un indirizzo non consentito")
    if len(r.content) > MAX_BYTE:
        raise RuntimeError("pagina troppo grande")
    return r


def tipo_da_titolo(titolo: str) -> str:
    return "bando" if TIPO_BANDO.search(titolo) else "normativa" if TIPO_NORMA.search(titolo) else "notizia"


def e_feed(r: requests.Response) -> bool:
    ct = r.headers.get("Content-Type", "").lower() if hasattr(r, "headers") else ""
    inizio = r.content[:400].lstrip().lower()
    return "xml" in ct or "rss" in ct or "atom" in ct or inizio.startswith((b"<?xml", b"<rss", b"<feed"))


def leggi_feed(xml: bytes) -> list[tuple[str, str, str, date | None]]:
    """Legge RSS 2.0, RSS 1.0 e Atom. Restituisce (titolo, link, testo, data)."""
    radice = ET.fromstring(xml)
    voci = []
    for el in radice.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        figli = {c.tag.split("}")[-1]: c for c in el}
        titolo = pulisci(figli["title"].text if "title" in figli else "")
        link = ""
        if "link" in figli:
            ln = figli["link"]
            link = (ln.text or "").strip() or ln.get("href", "")
        for c in el:
            if c.tag.split("}")[-1] == "link" and c.get("rel", "alternate") == "alternate" and c.get("href"):
                link = c.get("href"); break
        testo = ""
        for nome in ("encoded", "description", "summary", "content"):
            if nome in figli and (figli[nome].text or "").strip():
                testo = pulisci(figli[nome].text); break
        quando = None
        for nome in ("pubDate", "published", "updated", "date"):
            if nome in figli and figli[nome].text:
                t = figli[nome].text.strip()
                try:
                    quando = parsedate_to_datetime(t).astimezone(ROMA).date()
                except Exception:  # noqa: BLE001
                    quando = data_da_testo(t)
                if quando:
                    break
        if titolo and link:
            voci.append((titolo, link, testo, quando))
    return voci


def link_della_pagina(html_testo: str, base: str) -> list[tuple[str, str, str, date | None]]:
    """Senza feed: prende i link con un titolo significativo dal contenuto principale della pagina."""
    soup = BeautifulSoup(html_testo, "html.parser")
    for tag in soup(["nav", "header", "footer", "script", "style", "noscript", "form", "aside"]):
        tag.decompose()
    area = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.find("article") or soup.body or soup
    voci, visti = [], set()
    for a in area.find_all("a", href=True):
        testo = pulisci(a.get_text(" "))
        href = urljoin(base, a["href"]).split("#")[0]
        if len(testo) < 25 or not href.startswith("http") or href in visti or href.rstrip("/") == base.rstrip("/"):
            continue
        if re.search(r"\.(jpg|jpeg|png|gif|zip)$", href, re.I) or re.search(r"(cookie|privacy|login|accedi|facebook|twitter|linkedin|instagram|youtube)", href, re.I):
            continue
        visti.add(href)
        contesto = pulisci(a.find_parent(["li", "article", "div", "p"]).get_text(" ")) if a.find_parent(["li", "article", "div", "p"]) else testo
        voci.append((testo, href, contesto if contesto != testo else "", data_da_testo(contesto)))
    return voci


def controlla_fonte(fonte: dict) -> tuple[list[dict], dict]:
    """Restituisce le voci trovate e l'aggiornamento di stato per la tabella fonti."""
    url, fid = fonte["url"], fonte["id"]
    try:
        r = scarica_sicuro(url)
        tipo_rilevato, voci = None, []
        if e_feed(r):
            tipo_rilevato, voci = "rss", leggi_feed(r.content)
        else:
            pagina = r.text
            soup = BeautifulSoup(pagina, "html.parser")
            feed_url = None
            for ln in soup.find_all("link", href=True):
                tipo = (ln.get("type") or "").lower()
                if "rss" in tipo or "atom" in tipo:
                    feed_url = urljoin(r.url, ln["href"]); break
            candidati = [feed_url] if feed_url else []
            if "wp-content" in pagina or "wordpress" in pagina.lower():
                candidati.append(r.url.rstrip("/") + "/feed/")
            for c in candidati:
                try:
                    rf = scarica_sicuro(c)
                    if e_feed(rf):
                        v = leggi_feed(rf.content)
                        if v:
                            tipo_rilevato, voci = "rss", v
                            break
                except Exception:  # noqa: BLE001
                    continue
            if not voci:
                tipo_rilevato, voci = "pagina", link_della_pagina(pagina, r.url)
    except Exception as e:  # noqa: BLE001
        return [], {"stato": "errore", "messaggio": f"Sito non raggiungibile: {str(e)[:150]}", "voci_trovate": 0}

    limite = OGGI - timedelta(days=90)
    risultati = []
    for titolo, link, testo, quando in voci:
        if quando and quando < limite:
            continue
        link = urljoin(url, link)
        risultati.append({
            "tipo": tipo_da_titolo(titolo), "titolo": accorcia(titolo, 300),
            "sommario": accorcia(testo, 400) or None, "fonte": fonte["nome"], "url": link,
            "pubblicato_il": iso(min(quando or OGGI, OGGI)), "scadenza": iso(scadenza_da_testo(testo)) if testo else None,
            "settori": [], "id_esterno": f"f{fid}-" + hashlib.sha1(link.encode("utf-8")).hexdigest()[:16],
            "fonte_id": fid,
        })
        if len(risultati) >= MAX_VOCI_FONTE:
            break
    if not risultati:
        msg = ("Il feed non contiene novità degli ultimi 90 giorni." if tipo_rilevato == "rss"
               else "Nella pagina non abbiamo trovato link a notizie o avvisi: prova con l'indirizzo della pagina delle notizie o del feed RSS.")
        return [], {"stato": "errore" if tipo_rilevato == "pagina" else "ok", "messaggio": msg,
                    "tipo_rilevato": tipo_rilevato, "voci_trovate": 0}
    modo = "feed RSS" if tipo_rilevato == "rss" else "nuovi link della pagina"
    return risultati, {"stato": "ok", "tipo_rilevato": tipo_rilevato, "voci_trovate": len(risultati),
                       "messaggio": f"Controllata tramite {modo}: {len(risultati)} voci."}


# ---------------------------------------------------------------------------
# Scrittura su Supabase (API REST, chiave service_role / secret)
# ---------------------------------------------------------------------------
def normalizza_url(v: str) -> str:
    v = (v or "").strip().strip("'\"")
    m = re.search(r"supabase\.com/dashboard/project/([a-z0-9]+)", v, re.I)
    if m:
        return f"https://{m.group(1)}.supabase.co"
    if re.fullmatch(r"[a-z0-9]{20}", v, re.I):
        return f"https://{v}.supabase.co"
    if not re.match(r"^https?://", v, re.I):
        v = "https://" + v
    p = urlparse(v)
    return f"{p.scheme}://{p.netloc}"


class Database:
    def __init__(self, url: str, chiave: str):
        self.base = normalizza_url(url) + "/rest/v1"
        self.h = {"apikey": chiave, "Content-Type": "application/json", "User-Agent": UA}
        if chiave.startswith("eyJ"):  # chiave service_role in formato JWT (legacy)
            self.h["Authorization"] = f"Bearer {chiave}"

    def _req(self, metodo: str, percorso: str, **kw) -> requests.Response:
        r = requests.request(metodo, self.base + percorso, headers={**self.h, **kw.pop("headers", {})}, timeout=TIMEOUT, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase {metodo} {percorso} → {r.status_code}: {r.text[:300]}")
        return r

    def verifica(self) -> None:
        """Controlla indirizzo, chiave e versione del database prima di iniziare."""
        try:
            r = requests.get(self.base + "/novita", headers=self.h, timeout=TIMEOUT,
                             params={"select": "id,id_esterno,solo_parole,attivo,fonte_id", "limit": "1"})
        except Exception as e:  # noqa: BLE001
            raise SystemExit(f"ERRORE: Supabase non raggiungibile ({e}). Controlla il secret SUPABASE_URL.")
        if r.status_code in (401, 403):
            raise SystemExit("ERRORE: chiave non valida. Il secret SUPABASE_SERVICE_KEY deve contenere la chiave "
                             "'secret' (sb_secret_…) o 'service_role' del progetto, non la chiave pubblica.")
        if r.status_code == 404:
            raise SystemExit("ERRORE: indirizzo non valido o tabella mancante. Controlla il secret SUPABASE_URL "
                             "(solo https://…supabase.co) e che schema.sql sia stato eseguito.")
        if r.status_code == 400 and "column" in r.text.lower():
            raise SystemExit("ERRORE: il database non è aggiornato. In Supabase riesegui l'ultimo supabase/schema.sql "
                             f"(dettaglio: {r.text[:200]})")
        if r.status_code >= 400:
            raise SystemExit(f"ERRORE: risposta inattesa da Supabase ({r.status_code}): {r.text[:200]}")

    def disattiva_mancanti(self, fonte: str, presenti: list[str]) -> int:
        """Segna come non più attivi i bandi della fonte che non compaiono più tra quelli aperti."""
        if not presenti:
            return 0   # fonte vuota o non letta: non si disattiva nulla per prudenza
        elenco = ",".join('"' + x.replace('"', "") + '"' for x in presenti)
        r = self._req("PATCH", "/novita", params={"fonte": f"eq.{fonte}", "tipo": "eq.bando", "attivo": "is.true",
                                                  "id_esterno": f"not.in.({elenco})"},
                      data=b'{"attivo": false}', headers={"Prefer": "return=representation", "Accept": "application/json"})
        try:
            return len(r.json())
        except Exception:  # noqa: BLE001
            return 0

    def registra_esecuzione(self, tipo: str, esito: str, dettagli: list[dict], messaggio: str) -> None:
        self._req("POST", "/esecuzioni", data=json.dumps({"tipo": tipo, "esito": esito, "dettagli": dettagli,
                                                          "messaggio": messaggio}, ensure_ascii=False).encode("utf-8"),
                  headers={"Prefer": "return=minimal"})

    def rimuovi_esempi(self) -> None:
        self._req("DELETE", "/novita?esempio=eq.true", headers={"Prefer": "return=minimal"})

    def esistenti(self, fonte: str) -> set[str]:
        ids, passo, inizio = set(), 1000, 0
        while True:
            r = self._req("GET", "/novita", params={"select": "id_esterno", "fonte": f"eq.{fonte}",
                                                   "id_esterno": "not.is.null", "order": "id.asc"},
                          headers={"Range-Unit": "items", "Range": f"{inizio}-{inizio + passo - 1}"})
            blocco = r.json()
            ids.update(x["id_esterno"] for x in blocco)
            if len(blocco) < passo:
                return ids
            inizio += passo

    def tutti(self, tabella: str, select: str, filtri: dict | None = None) -> list[dict]:
        righe, passo, inizio = [], 1000, 0
        while True:
            r = self._req("GET", f"/{tabella}", params={"select": select, "order": "id.asc", **(filtri or {})},
                          headers={"Range-Unit": "items", "Range": f"{inizio}-{inizio + passo - 1}"})
            blocco = r.json()
            righe += blocco
            if len(blocco) < passo:
                return righe
            inizio += passo

    def sincronizza_preset(self) -> int:
        """Porta nel catalogo le fonti proposte per le sezioni (funzione del database)."""
        r = self._req("POST", "/rpc/sincronizza_fonti_preset", data=b"{}")
        try:
            return int(r.json())
        except Exception:  # noqa: BLE001
            return 0

    def aggiorna_fonte(self, fid: int, dati: dict) -> None:
        dati = {**dati, "ultimo_controllo": datetime.now(ROMA).isoformat()}
        self._req("PATCH", "/fonti", params={"id": f"eq.{fid}"},
                  data=json.dumps(dati, ensure_ascii=False).encode("utf-8"), headers={"Prefer": "return=minimal"})

    def scrivi(self, voci: list[dict], nuove: bool) -> None:
        """Nuove: inserite con la data di pubblicazione. Esistenti: aggiornate senza toccarla."""
        if not voci:
            return
        colonne = ["tipo", "titolo", "sommario", "fonte", "url", "scadenza", "settori", "id_esterno"]
        if nuove:
            colonne.insert(5, "pubblicato_il")
        for extra in ("fonte_id", "solo_parole", "attivo"):
            if all(extra in v for v in voci):
                colonne.append(extra)
        for i in range(0, len(voci), 200):
            blocco = [{k: v[k] for k in colonne} for v in voci[i:i + 200]]
            self._req("POST", "/novita", params={"on_conflict": "fonte,id_esterno", "columns": ",".join(colonne)},
                      data=json.dumps(blocco, ensure_ascii=False).encode("utf-8"),
                      headers={"Prefer": "resolution=merge-duplicates,return=minimal"})


# ---------------------------------------------------------------------------
# Esecuzione
# ---------------------------------------------------------------------------
FONTI = {   # prima le fonti più rapide, così un sito lento non blocca le altre
    "gu": (FONTE_GU, fonte_gu),
    "circolari": (FONTE_CIRCOLARI, fonte_circolari),
    "regione": (FONTE_REGIONE, fonte_regione),
    "incentivi": (FONTE_INCENTIVI, fonte_incentivi),
}


def stampa_prova(voci: list[dict]) -> None:
    for v in voci[:8]:
        log(f"    [{v['tipo']}] {v['pubblicato_il']} {v['titolo'][:90]}"
            f"{'  (scade ' + v['scadenza'] + ')' if v['scadenza'] else ''}"
            f"{'  → ' + ', '.join(v['settori']) if v['settori'] else ''}")
    if len(voci) > 8:
        log(f"    … e altre {len(voci) - 8}")


def salva(db: "Database", nome: str, voci: list[dict]) -> tuple[int, int]:
    gia = db.esistenti(nome)
    nuove = [v for v in voci if v["id_esterno"] not in gia]
    vecchie = [v for v in voci if v["id_esterno"] in gia]
    db.scrivi(nuove, nuove=True)
    db.scrivi(vecchie, nuove=False)
    return len(nuove), len(vecchie)


def fonti_personali(db: "Database | None", solo_nuove: bool, prova: bool) -> tuple[int, int, int]:
    """Controlla i siti aggiunti dalle imprese e quelli proposti per le sezioni.
    Restituisce (controllate, riuscite, voci)."""
    if db is None:
        elenco_fonti = [{"id": i + 1, "url": u, "nome": urlparse(u).hostname or u, "stato": "in_attesa"}
                        for i, u in enumerate(u.strip() for u in os.environ.get("URL_PROVA", "").split(",") if u.strip())]
        seguite = {f["id"] for f in elenco_fonti}
    else:
        if not solo_nuove:
            try:
                db.sincronizza_preset()
            except Exception as e:  # noqa: BLE001
                log(f"  fonti proposte per le sezioni non sincronizzate ({str(e)[:120]})")
        try:
            elenco_fonti = db.tutti("fonti", "id,url,nome,stato,preimpostata")
        except Exception:  # noqa: BLE001  (database non ancora aggiornato con i preset)
            elenco_fonti = db.tutti("fonti", "id,url,nome,stato")
        # le fonti proposte per le sezioni si controllano sempre, così sono pronte per chi si registra
        seguite = {f["id"] for f in elenco_fonti if f.get("preimpostata")}
        for p in db.tutti("profili", "id,fonti"):
            for f in p.get("fonti") or []:
                if isinstance(f, dict) and f.get("fonte_id") and f.get("attiva", True):
                    seguite.add(int(f["fonte_id"]))
    da_fare = [f for f in elenco_fonti if f["id"] in seguite and (not solo_nuove or f["stato"] == "in_attesa")]
    if not da_fare:
        log("  nessuna fonte aggiunta dalle imprese da controllare")
        return 0, 0, 0
    riuscite = totale = 0
    inizio_siti = time.monotonic()
    for n_fatti, f in enumerate(da_fare):
        if time.monotonic() - inizio_siti > LIMITE_SITI_TOTALE:
            log(f"  tempo a disposizione esaurito: {len(da_fare) - n_fatti} siti verranno controllati alla prossima esecuzione")
            da_fare = da_fare[:n_fatti]
            break
        try:
            with limite_tempo(LIMITE_SITO, f["nome"]):
                voci, stato = controlla_fonte(f)
        except TempoScaduto as e:
            voci, stato = [], {"stato": "errore", "messaggio": f"Il sito non ha risposto in tempo ({str(e)[:120]})", "voci_trovate": 0}
        voci = list({v["id_esterno"]: v for v in voci}.values())
        log(f"  • {f['nome']} ({f['url']}): {stato['messaggio']}")
        if prova:
            stampa_prova(voci)
        else:
            try:
                if voci:
                    n, a = salva(db, f["nome"], voci)
                    log(f"    {n} nuove, {a} aggiornate")
                db.aggiorna_fonte(f["id"], stato)
            except Exception as e:  # noqa: BLE001
                log(f"    ⚠ salvataggio non riuscito: {e}")
                continue
        riuscite += stato["stato"] == "ok"
        totale += len(voci)
    return len(da_fare), riuscite, totale


def main() -> int:
    prova = os.environ.get("PROVA", "").lower() in ("1", "true", "si", "sì", "yes")
    solo_nuove = os.environ.get("SOLO_NUOVE", "").lower() in ("1", "true", "si", "sì", "yes")
    scelte = [f.strip() for f in os.environ.get("FONTI", "").split(",") if f.strip()] or [*FONTI, "personali"]
    if solo_nuove:
        scelte = ["personali"]
    db = None
    if not prova:
        url, chiave = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not chiave:
            log("ERRORE: mancano SUPABASE_URL o SUPABASE_SERVICE_KEY (vanno nei secrets del repository).")
            return 2
        db = Database(url, chiave)
        db.verifica()
        if not solo_nuove:
            try:
                db.rimuovi_esempi()
            except Exception as e:  # noqa: BLE001
                log(f"Contenuti di esempio non rimossi ({str(e)[:120]})")
            try:
                parole = [k for p in db.tutti("profili", "id,parole_chiave") for k in (p.get("parole_chiave") or [])]
                imposta_parole_imprese(parole)
                log(f"Parole chiave delle imprese usate nella raccolta: {len(set(map(str.lower, parole)))}")
            except Exception as e:  # noqa: BLE001
                log(f"Parole chiave delle imprese non lette ({e}): uso solo i filtri standard")
    else:
        imposta_parole_imprese([p for p in os.environ.get("PAROLE_PROVA", "").split(",")])

    titolo = "controllo delle nuove fonti" if solo_nuove else "monitoraggio"
    log(f"{titolo.capitalize()} del {OGGI:%d/%m/%Y}{' — MODALITÀ PROVA, nessuna scrittura' if prova else ''}")
    riuscite, totale, n_fonti = 0, 0, 0
    dettagli: list[dict] = []
    controllate_personali = 0
    for chiave_fonte in scelte:
        if chiave_fonte == "personali":
            log("\n▸ Fonti aggiunte dalle imprese")
            try:
                controllate, ok, voci = fonti_personali(db, solo_nuove, prova)
                controllate_personali += controllate
                n_fonti += 1
                riuscite += 1  # l'esito di ogni singola fonte è registrato nella tabella fonti
                totale += voci
                if controllate:
                    log(f"  {ok}/{controllate} fonti funzionanti")
                dettagli.append({"fonte": "Fonti aggiunte e di settore", "esito": "ok",
                                 "messaggio": f"{ok} su {controllate} siti letti correttamente", "nuove": voci})
            except Exception as e:  # noqa: BLE001
                n_fonti += 1
                log(f"  ⚠ controllo non riuscito: {e}")
                dettagli.append({"fonte": "Fonti aggiunte e di settore", "esito": "errore", "messaggio": str(e)[:200]})
            continue
        nome, funzione = FONTI[chiave_fonte]
        n_fonti += 1
        log(f"\n▸ {nome}")
        inizio = time.monotonic()
        try:
            with limite_tempo(LIMITE_FONTE, nome):
                voci = funzione()
            voci = list({v["id_esterno"]: v for v in voci}.values())
            if prova:
                stampa_prova(voci)
                n = a = 0
            else:
                n, a = salva(db, nome, voci)
                msg = f"  {n} nuove, {a} aggiornate"
                if chiave_fonte == "incentivi":
                    try:
                        chiusi = db.disattiva_mancanti(nome, [v["id_esterno"] for v in voci])
                        if chiusi:
                            msg += f", {chiusi} non più aperti"
                    except Exception as e:  # noqa: BLE001  (le novità sono comunque salvate)
                        msg += f" (aggiornamento degli incentivi chiusi non riuscito: {str(e)[:100]})"
                log(msg)
            log(f"  ({time.monotonic() - inizio:.0f} s)")
            riuscite += 1
            totale += len(voci)
            dettagli.append({"fonte": nome, "esito": "ok", "messaggio": f"{len(voci)} voci", "nuove": n, "aggiornate": a})
        except (Exception, TempoScaduto) as e:  # noqa: BLE001
            log(f"  ⚠ fonte non raccolta dopo {time.monotonic() - inizio:.0f} s: {e}")
            dettagli.append({"fonte": nome, "esito": "errore", "messaggio": str(e)[:200]})
    esito = "ok" if riuscite == n_fonti else "parziale" if riuscite else "errore"
    riepilogo = f"{riuscite}/{n_fonti} fonti raccolte, {totale} voci elaborate."
    log(f"\nFatto: {riepilogo}")
    if db is not None and not prova and not (solo_nuove and not controllate_personali):
        try:
            db.registra_esecuzione("nuove_fonti" if solo_nuove else "completa", esito, dettagli, riepilogo)
        except Exception as e:  # noqa: BLE001
            log(f"(registro dell'esecuzione non salvato: {str(e)[:120]})")
    return 0 if riuscite else 1


if __name__ == "__main__":
    sys.exit(main())
