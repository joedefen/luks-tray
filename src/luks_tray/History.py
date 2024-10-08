#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" TBD """
# pylint: disable=invalid-name,broad-exception-caught


import sys
import json
from types import SimpleNamespace
from luks_tray.Utils import prt


class HistoryClass:
    """ TBD """
    def __init__(self, path):
        self.path = path
        self.dirty = False
        self.vitals = {}

    @staticmethod
    def make_ns(uuid):
        """ TBD """
        return SimpleNamespace(
                uuid=uuid,
                password='', # this is temporary and belongs in "secrets"
                delay_min=60,
                repeat_min=5,
                upon='', # "primary" mount only
            )

    def get_vital(self, uuid):
        """ Get vitals """
        vital = self.vitals.get(uuid, None)
        if not vital: # should not happen
            vital = self.make_ns(uuid)
        return vital

    def put_vital(self, vital):
        """ Put vitals """
        self.vitals[vital.uuid] = vital
        self.dirty = True
        self.save()

    def ensure_container(self, uuid, upon):
        """Ensure a discovered container is in the history"""
        if uuid not in self.vitals:
            ns = self.make_ns(uuid)
            ns.uuid, ns.upon = uuid, upon
            self.vitals[uuid] = ns
            self.dirty = True
        elif self.vitals[uuid].upon != upon and upon:
            self.vitals[uuid].upon = upon
            self.dirty = True

    def save(self):
        """ TBD """
        if not self.dirty:
            return
        entries = {}
        for uuid, vital in self.vitals.items():
            entries[uuid] = vars(vital)

        try:
            jason_str = json.dumps(entries)
            with open(self.path, 'w', encoding='utf-8') as f:
                f.write(jason_str + '\n')
            self.dirty = False
        except Exception as e:
            print(f"An error occurred while saving history: {e}", file=sys.stderr)


    def restore(self):
        """ TBD """
        try:
            with open(self.path, 'r', encoding='utf-8') as handle:
                entries = json.load(handle)
            self.vitals = {}
            for uuid, entry in entries.items():
                self.vitals[uuid] = SimpleNamespace(**entry)

            self.dirty = False
            return True

        except Exception as e:
            prt(f'restored picks FAILED: {e}')
            return True
