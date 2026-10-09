"""End-to-end tests of the decision/booking flow, in a real headless Chrome on a mock of the portal's results page."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException

from rlprenota.core import portal, runner
from rlprenota.core.deciders import TelegramDecider, TerminalDecider
from rlprenota.core.decisions import DecisionStore
from rlprenota.core.errors import LoginRejected
from rlprenota.core.models import Appointment, SearchPreferences
from rlprenota.telegram import TelegramBot
from tests.fakes import FakeTelegram, CHAT_ID, portal_page

PROV = "MILANO CITTA'"


def make_slot(when, sede="Ospedale Niguarda"):
    return {"when": when, "azienda": "ASST Grande Ospedale Metropolitano Niguarda", "comune": "MILANO", "sede": sede}


SLOTS = [
    make_slot("05/11/2026 - 08:30"),               # 0
    make_slot("05/11/2026 - 09:00"),               # 1 same place and day, other hour
    make_slot("06/11/2026 - 08:30", "Poliambulatorio Via Ricordi"),  # 2
    make_slot("20/12/2026 - 10:00"),               # 3
    make_slot("21/12/2026 - 10:00"),               # 4
    make_slot("22/12/2026 - 10:00"),               # 5 (page 2)
]


class FlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        cls.driver = webdriver.Chrome(options=options)
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()

    def setUp(self):
        self.state_file = os.path.join(self.tmp, f"{self._testMethodName}.json")
        self.fake = None

    def tearDown(self):
        if self.fake:
            self.fake.close()

    def load(self, slots=SLOTS):
        page = os.path.join(self.tmp, "portal.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(portal_page(slots))
        self.driver.get("file://" + page)

    def session(self, answers=None, current=None, dry_run=False, telegram=True, timeout_minutes=0.05,
                start="", end="", state_file=None, terminal_answers=None):
        prefs = SearchPreferences([PROV], start, end, 1, dry_run, telegram_timeout_minuti=timeout_minutes)
        if telegram:
            self.fake = self.fake or FakeTelegram()
            self.fake.answers = list(answers or [])
            decider = TelegramDecider(TelegramBot("TOKEN", CHAT_ID, api_url=self.fake.url), timeout_minutes)
        else:
            answers_iter = iter(terminal_answers or [])
            decider = TerminalDecider(ask=lambda question: next(answers_iter))
        store = DecisionStore("RICETTA1", state_file or self.state_file)
        s = runner.SearchSession(self.driver, None, prefs, store, decider,
                                 ignored_exceptions=(NoSuchElementException, StaleElementReferenceException))
        s.mode = portal.MODE_RESCHEDULE if current else portal.MODE_NEW
        s.current_appointment = Appointment(current, "Vecchio indirizzo", None) if current else None
        return s

    def booked_index(self):
        return self.driver.execute_script("return window.booked;")

    # --- first-time booking ---

    def test_new_booking_reject_then_accept_books_exactly_the_accepted_slot(self):
        self.load()
        s = self.session(answers=["r", "a"])
        self.assertEqual(runner.process_results(s, PROV), "STOP")
        self.assertEqual(self.booked_index(), 1)  # slot 0 rejected, slot 1 (other hour) proposed and booked

        first, second = self.fake.sent[0][1], self.fake.sent[1][1]
        for expected in ("05/11/2026", "08:30", "Ospedale Niguarda", "MILANO", PROV, "Prima prenotazione"):
            self.assertIn(expected, first)
        self.assertIn("09:00", second)
        self.assertTrue(any("PRENOTATO CON SUCCESSO" in text and "Portare la ricetta" in text for _, text, _ in self.fake.edits))
        self.assertTrue(any("Scartato" in text for _, text, _ in self.fake.edits))

        # A new run (restart) never proposes the rejected slot again
        self.load()
        s2 = self.session(answers=[None], state_file=self.state_file)
        self.fake.sent.clear()
        runner.process_results(s2, PROV)
        proposed = [text for _, text, buttons in self.fake.sent if buttons]
        self.assertTrue(proposed)
        self.assertFalse(any("08:30" in t and "05/11/2026" in t for t in proposed))

    def test_rejected_slots_are_skipped_across_pages(self):
        self.load()
        s = self.session(answers=["r", "r", "r", "r", "r", "a"])
        runner.process_results(s, PROV)
        self.assertEqual(self.booked_index(), 5)  # the only non-rejected slot is on page 2
        self.assertEqual(len(s.store.discarded), 5)

    def test_date_window_is_respected(self):
        self.load()
        s = self.session(answers=["a"], start="01/12/2026", end="31/12/2026")
        runner.process_results(s, PROV)
        self.assertEqual(self.booked_index(), 3)

    def test_radius_search_skips_far_slots_even_if_earlier(self):
        far = {"when": "01/11/2026 - 08:00", "azienda": "ASST Papa Giovanni XXIII", "comune": "BERGAMO", "sede": "Ospedale Papa Giovanni"}
        self.load([far] + SLOTS[:2])
        s = self.session(answers=["a"])
        s.filter = SearchPreferences([PROV], "", "", 1, location_mode="radius", home_comune="Milano", max_km=20).build_filter()
        runner.process_results(s, PROV)
        self.assertEqual(self.booked_index(), 1)  # the Bergamo slot (index 0) is ~47 km away
        self.assertNotIn("BERGAMO", self.fake.sent[0][1])

    # --- existing appointment ---

    def test_reschedule_only_proposes_earlier_slots(self):
        self.load()
        s = self.session(answers=["r", "r", "r"], current="10/11/2026 - 12:00")
        self.assertEqual(runner.process_results(s, PROV), "NEXT")
        self.assertEqual(len([1 for _, _, buttons in self.fake.sent if buttons]), 3)  # only the 3 November slots
        self.assertIsNone(self.booked_index())
        self.assertIn("Appuntamento attuale: 10/11/2026 - 12:00", self.fake.sent[0][1])

    def test_reschedule_accept_updates_current_appointment(self):
        self.load()
        s = self.session(answers=["a"], current="10/11/2026 - 12:00")
        runner.process_results(s, PROV)
        self.assertEqual(self.booked_index(), 0)
        self.assertEqual(s.current_appointment.date, "05/11/2026 - 08:30")

    # --- no answer / late answers ---

    def test_no_answer_then_late_accept_books_without_asking_again(self):
        self.load()
        s = self.session(answers=[None, None, None, None, None, None])
        self.assertEqual(runner.process_results(s, PROV), "NEXT")
        self.assertIsNone(self.booked_index())
        sent_before = len(self.fake.sent)
        first_message = self.fake.sent[0][0]
        # Expired proposals keep their buttons so the user can still answer
        self.assertTrue(any(mid == first_message and buttons and "Nessuna risposta" in text for mid, text, buttons in self.fake.edits))

        # Next cycle (a new search shows the list again): nothing new is proposed (no spam)...
        self.load()
        runner.process_results(s, PROV)
        self.assertEqual(len(self.fake.sent), sent_before)

        # ...then the user presses "Prenota" on the old message while the program waits between cycles
        self.fake.press(first_message, "a:" + s.store.slot_id_for_message(first_message))
        s.decider.idle(s, 1)
        self.load()
        self.assertEqual(runner.process_results(s, PROV), "STOP")
        self.assertEqual(self.booked_index(), 0)
        self.assertEqual(len(self.fake.sent), sent_before)  # booked without a new question

    def test_late_reject_discards(self):
        self.load()
        s = self.session(answers=[None, "r", "r", "r", "r", "r"])
        runner.process_results(s, PROV)
        first_message = self.fake.sent[0][0]
        self.fake.add_update({"message_reaction": {"chat": {"id": int(CHAT_ID)}, "message_id": first_message,
                                                   "new_reaction": [{"type": "emoji", "emoji": "👎"}]}})
        s.decider.idle(s, 1)
        self.assertEqual(len(s.store.discarded), 6)

    # --- dry run / terminal ---

    def test_dry_run_opens_the_summary_but_never_confirms(self):
        self.load()
        s = self.session(answers=["a"], dry_run=True)
        self.assertEqual(runner.process_results(s, PROV), "NEXT")
        self.assertIsNone(self.booked_index())
        self.assertEqual(self.driver.execute_script("return window.summariesOpened;"), [0])
        self.assertTrue(any("DRY RUN" in text for _, text, _ in self.fake.edits))

    def test_terminal_fallback_without_telegram(self):
        self.load()
        s = self.session(telegram=False, terminal_answers=["N", "", "S"])
        runner.process_results(s, PROV)
        self.assertEqual(self.booked_index(), 2)  # 0 rejected, 1 skipped, 2 accepted
        self.assertEqual(len(s.store.discarded), 1)

    def test_slot_gone_before_booking_fails_safely(self):
        self.load()
        s = self.session(answers=["a"])
        original = portal.book_slot

        def disappear_then_book(driver, *args):
            driver.execute_script("SLOTS.splice(0, 1); render();")
            return original(driver, *args)

        with mock.patch.object(portal, "book_slot", side_effect=disappear_then_book):
            self.assertEqual(runner.process_results(s, PROV), "RESET")
        self.assertIsNone(self.booked_index())
        self.assertTrue(any("non riuscita" in text for _, text, _ in self.fake.edits))


COMPLETA_DATI_AND_FORM = """<!doctype html><html><body>
<div id="modal" class="modal-dialog"><span class="modal-title">Completa dati ricetta</span>
  <input name="controllo" type="radio" onclick="window.controllo = true">
  <input name="controllo" type="radio" onclick="window.controllo = false">
  <button ng-click="prenotaCompletaDatiCtrl.conferma()" onclick="document.getElementById('modal').style.display='none';
     document.getElementById('form').style.display='block'">Conferma</button></div>
<div id="form" style="display:none"><select id="provincia"><option>MILANO CITTA'</option></select>
  <input id="telefono" type="text"><input id="email" type="text"><input id="consensoPrenotazione" type="checkbox"></div>
</body></html>"""


class EntryStepsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        cls.driver = webdriver.Chrome(options=options)
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()

    def load(self, html):
        page = os.path.join(self.tmp, "entry.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(html)
        self.driver.get("file://" + page)

    def test_detects_existing_appointment(self):
        self.load('<div class="dati-appuntamento-summary">05/11/2026 - 08:30</div>')
        self.assertEqual(portal.detect_booking_mode(self.driver, timeout=3), portal.MODE_RESCHEDULE)

    def test_detects_portal_error(self):
        self.load('<div class="modal-dialog"><span class="modal-title">Errore</span>Ricetta non trovata</div>')
        with self.assertRaises(LoginRejected) as raised:
            portal.detect_booking_mode(self.driver, timeout=3)
        self.assertIn("Ricetta non trovata", str(raised.exception))

    def test_first_booking_completes_data_and_contacts(self):
        self.load(COMPLETA_DATI_AND_FORM)
        self.assertEqual(portal.detect_booking_mode(self.driver, timeout=3), portal.MODE_NEW)
        prefs = SearchPreferences([PROV], "", "", 1, telefono="+39 333 123 4567", email="a@b.it", visita_controllo=False)
        portal.prepare_new_booking(self.driver, prefs, (NoSuchElementException, StaleElementReferenceException))
        self.assertIs(self.driver.execute_script("return window.controllo;"), False)
        self.assertEqual(self.driver.execute_script("return document.getElementById('telefono').value;"), "3331234567")
        self.assertEqual(self.driver.execute_script("return document.getElementById('email').value;"), "a@b.it")
        self.assertTrue(self.driver.execute_script("return document.getElementById('consensoPrenotazione').checked;"))

if __name__ == "__main__":
    unittest.main()
