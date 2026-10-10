#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты GUI-логики без X-сервера (offscreen)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gui"))

from faxpdf import hylafaxd as hx  # noqa: E402


class TestParsers(unittest.TestCase):
    def test_parse_jobs(self):
        out = """HylaFAX queue status (Tue 2026.10.10 12:00) at localhost
Jobs:
    JID  PHONE-NUMBER STATUS INFO
     11  +74951234567 "Отправка" page 1 of 3
     12  78121234567  "Ожидает"
Done:
"""
        jobs = hx.parse_jobs(out)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].jobid, "11")
        self.assertEqual(jobs[0].number, "+74951234567")

    def test_recv_name(self):
        p = Path("/var/spool/hylafax/recvq/recv.TIFF.3335551122.4951234567.001")
        rx = hx._parse_recv_name(p)
        self.assertEqual(rx.sender_tsi, "3335551122")
        self.assertEqual(rx.sender_csid, "4951234567")

    def test_list_incoming_empty(self):
        with tempfile.TemporaryDirectory() as d:
            spool = Path(d)
            old_recvq, old_doneq = hx.RECVQ, hx.DONEQ
            try:
                hx.RECVQ = spool / "recvq"
                hx.DONEQ = spool / "doneq"
                hx.RECVQ.mkdir()
                (hx.RECVQ / "readme.txt").write_text("x")
                (hx.RECVQ / "recv.TIFF.111.222.001").write_bytes(b"II*\x00")
                items = hx.list_incoming()
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0].sender_csid, "222")
            finally:
                hx.RECVQ, hx.DONEQ = old_recvq, old_doneq

    def test_hylafax_bin_none(self):
        # несуществующий бинарник -> None, без исключения
        self.assertIsNone(hx.hylafax_bin("definitely-not-a-real-binary-xyz"))


class TestGuiImport(unittest.TestCase):
    def test_window_construct(self):
        import faxpdf.app  # noqa: F401
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        w = faxpdf.app.MainWindow()
        titles = [w.tabs.tabText(i) for i in range(w.tabs.count())]
        self.assertIn("📥 Приём", titles)
        self.assertIn("📤 Отправка", titles)
        w.worker.stop()
        w.worker.wait(3000)
        w.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
