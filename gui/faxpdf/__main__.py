#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Точка входа пакета fax-pdf (запуск GUI)."""
import sys


def main():
    from faxpdf.app import run
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
