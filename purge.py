from mapreduce import operation as op
import logging
import time

def purge_sms(sms):
  now = time.time() * 1000
  two_weeks_ago = now - (2 * 7 * 24 * 60 * 60 * 1000)
  if sms.date < two_weeks_ago:
    yield op.db.Delete(sms)
