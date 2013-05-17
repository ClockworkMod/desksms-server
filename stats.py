from mapreduce import operation as op
import logging

def get_registration_stats(registration):
  if not registration.registration_id:
    return

  if registration.registration_id.startswith("apple:") or registration.registration_id.startswith("ios:"):
    yield op.counters.Increment("iOS")
    if registration.subscription_expiration > registration.registration_date + 30 * 24 * 60 * 60 * 1000:
       yield op.counters.Increment('iOS-Paid')
  else:
    yield op.counters.Increment("Android")

def force_email_off(registration):
    if registration.forward_email:
        registration.forward_email = False
        yield op.db.Put(registration)