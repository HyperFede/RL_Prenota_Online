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

### DOVE (in alternativa alle province) ###
modalita_luogo = "provinces"             # "provinces" | "radius" (entro N km) | "facilities" (strutture scelte)
comune_di_partenza = "Milano"            # per "radius": il tuo comune (nessun indirizzo esatto)
distanza_massima_km = 25                 # per "radius": da 1 a 200 km
strutture = [                            # per "facilities": nome (anche parziale) e provincia
    # {"name": "Niguarda", "province": "MILANO CITTA'"},
]

### QUANDO ###
giorni_settimana = None                  # es. {0, 1, 2, 3, 4} = da lunedì a venerdì; None = tutti
orario_da = None                         # es. "08:00"; None = nessun limite
orario_a = None                          # es. "12:30"

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
