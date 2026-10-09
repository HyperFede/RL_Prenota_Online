### ISTRUZIONI ###
# Per utilizzare questo file al posto dell'inserimento manuale tramite prompt dei comandi,
# sostituisci i valori delle variabili sotto e rinomina il file in 'data_file.py'.


codice_fiscale = "RSSMRA80A01F205X"
tessera_sanitaria = "12345"              # ultime 5 cifre della tessera sanitaria
prescription_n = "0300A1234567890"       # codice della ricetta
province = ["MILANO CITTA'", "MONZA E DELLA BRIANZA"]
start_date = "01/01/2027"                # gg/mm/aaaa, "" = nessun limite
end_date = "31/03/2027"                  # gg/mm/aaaa, "" = nessun limite
refresh_frequency = 300                  # secondi di pausa tra un ciclo di ricerca e il successivo
dry_run = True                           # True = verifica soltanto, non prenota e non modifica nulla

### SOLO PER UNA PRIMA PRENOTAZIONE ###
# Se la ricetta non ha ancora un appuntamento il programma lo rileva da solo e cerca una prima data.
# In questo caso il portale chiede un recapito telefonico (obbligatorio) e, per alcune ricette,
# se si tratta di una visita di CONTROLLO / FOLLOW-UP.
telefono = "3331234567"
email = ""                               # opzionale
visita_controllo = None                  # True / False, None = chiedimelo sul terminale se il portale lo richiede

### TELEGRAM (opzionale) ###
telegram_bot_token = ""
telegram_chat_id = ""
telegram_timeout_minuti = 15             # quanto attendere la tua risposta prima di riprendere la ricerca
continua_dopo_prenotazione = False       # True = dopo aver prenotato continua a cercare una data ancora migliore

### PROVINCE ###
"""
BERGAMO
BRESCIA
COMO
CREMONA
LECCO
LODI
MANTOVA
MILANO CITTA'
MILANO PROVINCIA
MONZA E DELLA BRIANZA
PAVIA
SONDRIO
VARESE
"""
