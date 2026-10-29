# SBS · SSN Booking System

**SBS** automatizza la ricerca di appuntamenti nel portale di prenotazione online delle visite ed esami del Servizio Sanitario di Regione Lombardia. Controlla le disponibilità al posto tuo, ti avvisa su Telegram quando trova una data migliore e, se vuoi, la prenota da solo.

---

## Perché

Le attese nel SSN possono durare mesi, a volte più di un anno. Spesso però si liberano posti all'improvviso, quando altri cittadini spostano o annullano un appuntamento, e restano disponibili per pochi minuti.
Accorgersene vuol dire entrare nel portale e aggiornare la pagina più volte al giorno: un secondo lavoro che pochi possono permettersi, e che pesa soprattutto su chi ha più bisogno di cure.

SBS fa questo lavoro in modo continuo e automatico, seguendo esattamente gli stessi passaggi che faresti tu sul portale.

---

## Funzionalità principali

| | |
|---|---|
| 🔎 **Ricerca continua** | Controlla il portale a intervalli regolari, con la frequenza che preferisci. |
| 📅 **Prima prenotazione o spostamento** | Riconosce da solo se la ricetta ha già un appuntamento: in quel caso cerca una data migliore e lo sposta, altrimenti cerca la prima data utile e la prenota. |
| ⏪ ⏩ **Anticipa, posticipa o entrambi** | Cerca date prima dell'appuntamento attuale, dopo, oppure in entrambe le direzioni. |
| 📍 **Dove** | Per province, entro una distanza dal tuo comune oppure solo in strutture precise. |
| 🕒 **Quando** | Intervallo di date, giorni della settimana e fascia oraria. |
| 💬 **Telegram** | Ogni disponibilità migliore arriva come messaggio con i pulsanti **Prenota** e **Scarta**. |
| 🤖 **Prenotazione automatica** | Su tua conferma prenota o sposta l'appuntamento e ti invia l'esito, con le eventuali note di preparazione. |
| 🧪 **Modalità prova** | Con il *dry run* arriva fino al riepilogo sul portale senza prenotare nulla. |
| 🔒 **Privacy** | I dati restano sul tuo computer e i log non contengono dati personali in chiaro. |

---

## Installazione

**Requisiti**
- Python 3;
- Google Chrome.

Scarica il progetto e installa le dipendenze da terminale, nella cartella principale:

```bash
git clone https://github.com/HyperFede/RL_Prenota_Online.git
```

```bash
cd RL_Prenota_Online
```

```bash
pip install -r requirements.txt
```

---

## Utilizzo

Avvia SBS dalla cartella principale:

```bash
python main.py
```

Puoi inserire i dati in due modi:

1. **A mano**: il programma ti chiede uno per uno codice fiscale, tessera sanitaria, ricetta, province, date e opzioni.
2. **Da file** (consigliato): copia `data_file_template.py` in `data_file.py`, compilalo e scegli l'opzione **2** all'avvio. Il file permette di usare tutte le opzioni descritte sotto.

```bash
cp data_file_template.py data_file.py
```

Il programma apre il browser e lo lascia visibile, così puoi seguire ogni passaggio. Per fermarlo premi `Ctrl+C`.

---

## Come funziona una ricerca

1. **Accesso al portale** con codice fiscale, ultime 5 cifre della tessera sanitaria e codice della ricetta.
2. **Riconoscimento della situazione**
   - la ricetta ha già un appuntamento → cerca una data *migliore* secondo la modalità scelta;
   - la ricetta non ha ancora un appuntamento → cerca la *prima* data disponibile.
3. **Ricerca** in ogni provincia indicata, applicando i filtri di luogo, data e orario.
4. **Proposta**: per ogni appuntamento migliore ricevi un messaggio con data, ora, struttura, comune e provincia.
5. **Decisione**
   - **✅ Prenota**: SBS prenota (o sposta) proprio quell'appuntamento e ti conferma l'esito;
   - **❌ Scarta**: l'appuntamento non ti verrà più proposto e la ricerca continua.
6. **Fine**: dopo la prenotazione la ricerca si conclude, a meno che tu non abbia scelto di continuare a cercare una data ancora migliore.

> Un appuntamento è identificato da data, ora, struttura e provincia: un orario o una sede diversi sono considerati un appuntamento nuovo.

Se non rispondi entro il tempo impostato, la ricerca riprende senza riproporre la stessa data. Puoi comunque premere **Prenota** più tardi: l'appuntamento verrà prenotato appena SBS lo ritrova disponibile.

Le scelte (appuntamenti accettati o scartati) vengono salvate in `stato_ricerca.json`, senza il codice della ricetta. Cancellalo per ripartire da zero.

---

## Configurazione (`data_file.py`)

### Ricetta e recapiti

| Opzione | Descrizione |
|---|---|
| `codice_fiscale` | il tuo codice fiscale |
| `tessera_sanitaria` | le ultime 5 cifre della tessera sanitaria |
| `prescription_n` | il codice della ricetta |
| `telefono` | obbligatorio solo per una prima prenotazione |
| `email` | facoltativa |
| `visita_controllo` | `True` / `False` se la ricetta riporta *CONTROLLO* o *FOLLOW-UP*; `None` = chiedimelo se serve |

### Dove

| `modalita_luogo` | Cosa cerca | Opzioni collegate |
|---|---|---|
| `"provinces"` | nelle province indicate | `province`, ad esempio `["MILANO CITTA'", "COMO"]` |
| `"radius"` | entro una distanza dal tuo comune (in linea d'aria) | `comune_di_partenza`, `distanza_massima_km` |
| `"facilities"` | solo nelle strutture indicate | `strutture` |

Per le strutture basta una parte del nome come appare sul portale, insieme alla provincia:

```python
strutture = [
    {"name": "Niguarda", "province": "MILANO CITTA'"},
    {"name": "Villa Aprica", "province": "COMO"},
]
```

L'elenco delle province disponibili è in fondo a `data_file_template.py`.

### Quando

| Opzione | Descrizione |
|---|---|
| `start_date`, `end_date` | intervallo di date accettabile (`gg/mm/aaaa`, `""` = nessun limite) |
| `giorni_settimana` | ad esempio `{0, 1, 2, 3, 4}` = da lunedì a venerdì; `None` = tutti |
| `orario_da`, `orario_a` | fascia oraria, ad esempio `"08:00"` e `"12:30"`; `None` = nessun limite |
| `direzione` | rispetto all'appuntamento già prenotato, vedi sotto |

| `direzione` | Cosa cerca |
|---|---|
| `"anticipa"` | solo date precedenti all'appuntamento attuale (predefinita) |
| `"posticipa"` | solo date successive, fino a `end_date` (se vuota: un mese dopo l'appuntamento) |
| `"entrambe"` | prima o dopo, proponendo per prima la data più vicina a oggi |

Senza un appuntamento già prenotato, `"anticipa"` ed `"entrambe"` cercano semplicemente la prima data utile.

### Opzioni

| Opzione | Descrizione |
|---|---|
| `refresh_frequency` | secondi di pausa tra un controllo e il successivo |
| `dry_run` | `True` = solo prova, non prenota e non modifica nulla |
| `continua_dopo_prenotazione` | `True` = dopo aver prenotato continua a cercare una data migliore |

---

## Notifiche Telegram (facoltative)

1. Crea un bot con **@BotFather** su Telegram e copia il token.
2. Scrivi un messaggio al bot, poi apri `https://api.telegram.org/bot<TOKEN>/getUpdates` per ricavare il tuo Chat ID.
3. Inserisci i valori in `data_file.py`:

| Opzione | Descrizione |
|---|---|
| `telegram_bot_token` | il token del bot |
| `telegram_chat_id` | il tuo Chat ID |
| `telegram_timeout_minuti` | quanto attendere la tua risposta prima di riprendere la ricerca |

Oltre ai pulsanti puoi rispondere con una reazione 👍 / 👎 o scrivendo «si» / «no» in risposta al messaggio.
Senza Telegram, la conferma viene chiesta direttamente sul terminale.

---

## Affidabilità

- **Errori temporanei del portale** (lentezza, sessione scaduta, «riprova più tardi») vengono riprovati automaticamente, senza interrompere la ricerca.
- **Errori nei dati** (ricetta non valida o già utilizzata, codice fiscale errato) fermano la ricerca con un messaggio chiaro.
- **Risultati affidabili**: prima di leggere le disponibilità, SBS verifica che la pagina mostri davvero i risultati aggiornati della provincia cercata, così ogni appuntamento ha la provincia corretta e una data già scartata non viene riproposta.

---

## Privacy

- I dati personali restano sul tuo computer e vengono inviati solo al portale regionale, esattamente come faresti tu a mano.
- I log non contengono mai dati personali in chiaro.
- Le distanze sono calcolate localmente sulle coordinate dei comuni lombardi: nessun indirizzo viene inviato a terzi.
- `data_file.py` contiene i tuoi dati: è già escluso da git, non condividerlo.

---

## Per sviluppatori

Il codice è nel pacchetto `rlprenota/`:

| Modulo | Contenuto |
|---|---|
| `core/portal.py` | i passaggi sul portale (Selenium) |
| `core/runner.py` | il ciclo di ricerca e la scelta della data migliore |
| `core/deciders.py` | come vengono chieste le conferme (Telegram o terminale) |
| `core/filters.py`, `geo/` | filtri di luogo, data e orario |
| `core/errors.py`, `core/redact.py` | classificazione degli errori e mascheramento dei dati nei log |

**Test**

```bash
pip install -r requirements-dev.txt
```

```bash
pytest
```

I test usano un finto server Telegram e copie semplificate delle pagine del portale (serve Chrome).
La tabella dei comuni si rigenera con `python scripts/build_comuni.py` (dati © OpenStreetMap contributors, licenza ODbL).

---

## Avvertenze

SBS non è un servizio ufficiale di Regione Lombardia e non è affiliato al portale di prenotazione. Usalo in modo responsabile: ogni ricerca occupa il portale come farebbe una persona e ogni prenotazione è reale, salvo in modalità prova.

## Origine del progetto

SBS nasce come fork di [RL Prenota Online](https://github.com/Ludovik95/RL_Prenota_Online) di Ludovica Bonaldo, uno script da riga di comando che automatizzava la ricerca sul portale regionale. Un ringraziamento all'autrice per l'idea e il punto di partenza.

Da allora il progetto è stato riscritto in ogni sua parte: logica di ricerca, gestione degli errori, filtri, notifiche e prenotazione su Telegram, sicurezza e test.

## Licenza

SBS è distribuito con licenza [MIT](LICENSE): puoi usarlo, modificarlo e ridistribuirlo liberamente, anche per scopi commerciali, a condizione di mantenere l'avviso di copyright e il testo della licenza. Il software è fornito «così com'è», senza alcuna garanzia.

Il file [LICENSE](LICENSE) riporta entrambi gli avvisi di copyright: quello di SBS e quello del progetto originale, come richiesto dalla licenza MIT per il codice derivato.
