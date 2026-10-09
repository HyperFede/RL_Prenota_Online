# RL Prenota Online
## Che cos'è RL Prenota Online
La sanità in Lombardia sta diventando sempre più privata, e le disponibilità di visita nel SSN sempre più limitate. 
Molto spesso chi deve prenotare delle visite di controllo deve aspettare molti mesi, se non un anno e più, e per le urgenze si è obbligati a rivolgersi a cliniche private con ***costi allucinanti***. 
Quando si hanno molte patologie, queste pratiche diventano dispendiose in termini di tempo e soldi, e questo ***danneggia la parte più vulnerabile della popolazione***.

Alcune volte capita che delle visite tornino disponibili per modifiche di appuntamento di altri cittadini. Non sempre è semplice individuarli e prenotarli per tempo: la maggior parte delle disponibilità spariscono dopo poco perché qualcun altro è stato più veloce. 
L'unico modo per non avere la visita dopo un anno è entrare sul sito di prenotazione ogni giorno e continuare ad aggiornare la pagina, nella speranza che si liberi un posto. Oppure chiamando il numero regionale del CUP continuamente. Insomma, un secondo lavoro che ***in pochissimi possono permettersi***.

RL Prenota Online permette di automatizzare la ricerca di nuovi appuntamenti nel sistema di prenotazione online delle visite mediche in Regione Lombardia. 
I dati rimangono nel proprio device e non vengono trasmessi a terzi se non durante l'immissione nel sito di prenotazione (quello che accadrebbe facendo i passaggi manualmente).


## Installazione dei prerequisiti
Per utilizzare RL Prenota Online bisogna installare sul proprio computer Python e Selenium.
Una volta installato Python e pip, basterà aprire il prompt dei comandi e digitare 
```
pip install -U selenium certifi
```


## Come usarlo
Apri un prompt dei comandi nella cartella principale e avvia il codice digitando 
```
py main.py
```
Ti verranno chiesti il codice fiscale, gli ultimi 5 numeri della tessera sanitaria e il codice della ricetta (15 caratteri alfanumerici se ricetta elettronica, 15 cifre se ricetta rossa). 
In alternativa copia `data_file_template.py` in `data_file.py`, compilalo e scegli l'opzione 2 all'avvio.

Dopo l'accesso il programma capisce da solo cosa fare:
- **la ricetta ha già un appuntamento** → cerca una data *precedente* a quella attuale (e compresa tra le date indicate) e, se la accetti, sposta l'appuntamento;
- **la ricetta non ha ancora un appuntamento** → cerca la *prima* data disponibile tra le date indicate e, se la accetti, la prenota. In questo caso il portale chiede un numero di telefono (obbligatorio), un'email (facoltativa) e, per alcune ricette, se si tratta di una visita di CONTROLLO/FOLLOW-UP.

Con `dry_run` attivo il programma apre il riepilogo dell'appuntamento sul portale ma **non** prenota né modifica nulla.

## Notifiche e conferma da Telegram
Se configuri un bot Telegram (crealo con @BotFather e ricava il tuo Chat ID da `https://api.telegram.org/bot<TOKEN>/getUpdates`), per ogni disponibilità migliore riceverai un messaggio con data, ora, struttura, azienda, comune e provincia, e due pulsanti:
- **✅ Prenota**: il programma prenota (o sposta) automaticamente proprio quell'appuntamento e ti conferma l'esito con le eventuali note di preparazione;
- **❌ Scarta**: l'appuntamento viene ignorato per sempre (anche dopo un riavvio) e la ricerca continua con le altre disponibilità.

Puoi rispondere anche con una reazione 👍 / 👎 al messaggio o rispondendo al messaggio con "si" / "no".
Un appuntamento è identificato da data, ora, struttura e provincia: un orario, un giorno o una sede diversi sono un appuntamento diverso e ti verranno proposti.

Se non rispondi entro `telegram_timeout_minuti` la ricerca riprende e quella proposta non viene ripetuta; puoi però rispondere anche più tardi: se premi **Prenota** l'appuntamento verrà prenotato appena il programma lo ritrova disponibile.
Senza Telegram la stessa scelta viene chiesta sul terminale.

Le scelte sono salvate nel file `stato_ricerca.json` (per ricetta, senza salvarne il codice). Cancellalo per ripartire da zero.

Per provare il bot: `py telegram_bot.py` (legge token e chat id da `data_file.py`) invia un messaggio di prova con i pulsanti.

## Test
```
py -m unittest discover tests
```
I test usano un finto server Telegram e una copia semplificata della pagina dei risultati del portale (serve Chrome).
