import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rlprenota.core.models import Slot
from rlprenota.core.decisions import DecisionStore, EXPIRED, ACCEPTED, BOOKED, ACCEPT, REJECT
from rlprenota.telegram import TelegramBot
from tests.fakes import FakeTelegram, CHAT_ID


def slot(when="10/11/2026 09:00", sede="Ospedale Niguarda", comune="MILANO", azienda="ASST Niguarda", prov="MILANO CITTA'"):
    return Slot(datetime.strptime(when, "%d/%m/%Y %H:%M"), azienda, sede, comune, prov)


class SlotIdentityTest(unittest.TestCase):
    def test_same_appointment_same_id(self):
        self.assertEqual(slot().slot_id, slot(sede="  ospedale   NIGUARDA ").slot_id)

    def test_any_difference_is_another_appointment(self):
        base = slot().slot_id
        self.assertNotEqual(base, slot(when="10/11/2026 09:30").slot_id)    # other hour
        self.assertNotEqual(base, slot(when="11/11/2026 09:00").slot_id)    # other date
        self.assertNotEqual(base, slot(sede="Ospedale San Carlo").slot_id)  # other location
        self.assertNotEqual(base, slot(prov="MILANO PROVINCIA").slot_id)    # other province

    def test_round_trip(self):
        s = slot()
        self.assertEqual(Slot.from_dict(s.to_dict()).slot_id, s.slot_id)


class DecisionStoreTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "stato.json")

    def test_discard_is_persisted_and_scoped_to_the_prescription(self):
        store = DecisionStore("ABC123", self.path)
        store.record_proposal(slot(), 7)
        store.discard(slot())
        self.assertFalse(store.should_propose(slot()))

        reloaded = DecisionStore("ABC123", self.path)
        self.assertTrue(reloaded.is_discarded(slot()))
        self.assertFalse(reloaded.should_propose(slot()))
        self.assertTrue(reloaded.should_propose(slot(when="10/11/2026 09:30")))

        other_prescription = DecisionStore("ZZZ999", self.path)
        self.assertTrue(other_prescription.should_propose(slot()))
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn("ABC123", f.read())

    def test_expired_not_reproposed_in_session_but_after_restart(self):
        store = DecisionStore("ABC123", self.path)
        store.record_proposal(slot(), 7)
        self.assertFalse(store.should_propose(slot()))  # pending
        store.set_status(slot().slot_id, EXPIRED)
        self.assertFalse(store.should_propose(slot()))
        self.assertTrue(DecisionStore("ABC123", self.path).should_propose(slot()))

    def test_late_accept_and_booked(self):
        store = DecisionStore("ABC123", self.path)
        store.record_proposal(slot(), 7)
        self.assertEqual(store.slot_id_for_message(7), slot().slot_id)
        store.set_status(slot().slot_id, ACCEPTED)
        self.assertTrue(store.is_pre_accepted(slot()))
        store.set_status(slot().slot_id, BOOKED)
        self.assertFalse(store.is_pre_accepted(slot()))
        self.assertFalse(store.should_propose(slot()))


class TelegramBotTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTelegram()
        self.bot = TelegramBot("TOKEN", CHAT_ID, api_url=self.fake.url)

    def tearDown(self):
        self.fake.close()

    def test_only_https_api(self):
        for url in ("http://api.telegram.org", "file:///etc/passwd", "ftp://example.com"):
            with self.assertRaises(ValueError):
                TelegramBot("TOKEN", CHAT_ID, api_url=url)

    def test_certificate_errors_never_fall_back_to_insecure_tls(self):
        import ssl
        import urllib.error
        from unittest import mock
        from rlprenota.telegram import TelegramError
        bot = TelegramBot("TOKEN", CHAT_ID)
        failure = urllib.error.URLError(ssl.SSLCertVerificationError("self-signed certificate"))
        with mock.patch("urllib.request.urlopen", side_effect=failure) as urlopen:
            with self.assertRaises(TelegramError):
                bot.send("x")
        self.assertEqual(urlopen.call_count, 1)  # no retry without verification

    def test_send_with_buttons(self):
        message_id = self.bot.send("hello", TelegramBot.decision_buttons("abc"))
        self.assertEqual(self.fake.sent, [(message_id, "hello", True)])

    def test_buttons_reactions_and_replies(self):
        self.fake.press(100, "a:abc")
        self.fake.press(101, "r:def")
        self.fake.press(102, "a:xyz", chat_id="999")  # someone else: ignored
        self.fake.add_update({"message_reaction": {"chat": {"id": int(CHAT_ID)}, "message_id": 103,
                                                   "new_reaction": [{"type": "emoji", "emoji": "👎"}]}})
        self.fake.add_update({"message_reaction": {"chat": {"id": int(CHAT_ID)}, "message_id": 104,
                                                   "new_reaction": [{"type": "emoji", "emoji": "👍"}]}})
        self.fake.add_update({"message": {"chat": {"id": int(CHAT_ID)}, "text": "Sì!",
                                          "reply_to_message": {"message_id": 105}}})
        self.fake.add_update({"message": {"chat": {"id": int(CHAT_ID)}, "text": "ciao"}})  # not a reply
        self.fake.press(106, "l:loginrequest:y")  # another kind of button: not a decision

        decisions = self.bot.poll()
        simplified = [(d["decision"], d.get("slot_id"), d.get("message_id")) for d in decisions]
        self.assertEqual(simplified, [(ACCEPT, "abc", 100), (REJECT, "def", 101), (REJECT, None, 103),
                                      (ACCEPT, None, 104), (ACCEPT, None, 105)])
        self.assertEqual(self.bot.poll(), [])  # offset advanced: nothing delivered twice


class ProvinceMatchTest(unittest.TestCase):
    def test_tolerant_but_unambiguous(self):
        from rlprenota.core import portal

        class Option:
            def __init__(self, text):
                self.text = text

        options = [Option(t) for t in ["", "BERGAMO", "MILANO CITTA'", "MILANO PROVINCIA", "MONZA E DELLA BRIANZA"]]
        match = lambda name: (portal.match_province_option(options, name) or Option(None)).text
        self.assertEqual(match("MILANO CITTA"), "MILANO CITTA'")
        self.assertEqual(match("Milano Città"), "MILANO CITTA'")
        self.assertEqual(match("monza"), "MONZA E DELLA BRIANZA")
        self.assertIsNone(match("MILANO"))  # ambiguous
        self.assertIsNone(match("ROMA"))

if __name__ == "__main__":
    unittest.main()
