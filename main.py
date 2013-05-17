#!/usr/bin/env python
#
# Copyright 2007 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from google.appengine.ext import webapp
import webapp2
from google.appengine.ext.webapp import util
from google.appengine.ext import db

import json as simplejson
import unicodedata
import time

from google.appengine.api import users

import logging
import email

from google.appengine.ext.webapp.mail_handlers import InboundMailHandler 

import urllib
import urllib2

from google.appengine.api import xmpp
from google.appengine.api import mail
from google.appengine.api.mail import EncodedPayload

import re
from google.appengine.api import oauth
import traceback

from google.appengine.ext.db import Property
from google.appengine.api import memcache

from userobject import UserObject
from userobject import UserObjectHandler
import random
import base64
import hashlib

from handlers import APIHandler
from google.appengine.api import urlfetch

class User(db.Model):
    registration_id = db.StringProperty(indexed = False)
    forward_xmpp = db.BooleanProperty(indexed = False)
    forward_email = db.BooleanProperty(indexed = False)
    forward_web = db.BooleanProperty(indexed = False)
    version_code = db.IntegerProperty()
    subscription_expiration = db.IntegerProperty(indexed = False)
    registration_date = db.IntegerProperty()
    promotion = db.StringProperty(indexed = False)
    promotion_date = db.IntegerProperty(indexed = False)

class UserContact(db.Model):
    number = db.StringProperty(indexed = False)

class UserContactSubscribed(db.Model):
    pass

class UserContactLastMessage(object):
    # is this stuff necessary? I guess so if sync is off.
    last_message = None
    last_message_name = None
    last_message_number = None

class Sms(UserObject):
    message = db.StringProperty(multiline = True, indexed = False)
    #seen = db.IntegerProperty()
    type = db.IntegerProperty(indexed = False)
    date = db.IntegerProperty()
    #id = db.IntegerProperty()
    number = db.StringProperty(indexed = False)
    image = db.BlobProperty(default = None)
    #read = db.BooleanProperty()
    #thread_id = db.IntegerProperty()
    name = db.StringProperty(indexed = False)
    
    type.json_serialize = False
    image.json_serialize = False
    
    def json_serialize(self, data):
        UserObject.json_serialize(self, data)
        if self.type == 2:
            data['type'] = 'outgoing'
        elif self.type == 1:
            data['type'] = 'incoming'
        elif self.type == 3:
            data['type'] = 'pending'

        if self.image:
            data['image'] = True

    def json_deserialize(self, data):
        type = data.get('type', None)
        if type == 'incoming':
            self.type = 1
        elif type == 'outgoing':
            self.type = 2
        elif type == 'pending':
            self.type = 3
        elif type == None:
            # HACK: I had a bug in the original client that never set
            # a type on proxied/outgoing messages
            self.type = 2

        image = data.get('image', None)
        if image:
            self.image = base64.decodestring(image)

class OutboxSms(UserObject):
    # need to ensure this is multiline
    message = db.StringProperty(multiline = True, indexed = False)
    date = db.IntegerProperty()
    number = db.StringProperty(indexed = False)

class Call(UserObject):
    #id = db.IntegerProperty()
    number = db.StringProperty(indexed = False)
    date = db.IntegerProperty()
    duration = db.IntegerProperty(indexed = False)
    type = db.IntegerProperty(indexed = False)
    name = db.StringProperty(indexed = False)

    type.json_serialize = False

    
    def json_serialize(self, data):
        UserObject.json_serialize(self, data)
        if self.type == 2:
            data['type'] = 'outgoing'
        elif self.type == 1:
            data['type'] = 'incoming'
        elif self.type == 3:
            data['type'] = 'missed'
            
    def json_deserialize(self, data):
        type = data.get('type', None)
        if type == 'incoming':
            self.type = 1
        elif type == 'outgoing':
            self.type = 2
        elif type == 'missed':
            self.type = 3
        else:
            # HACK: I had a bug in the original client that never set
            # a type on proxied/outgoing messages
            self.type = 2

class LoginHandler(APIHandler):
    def get(self):
        email = self.check_authorization(False)
        continue_url = self.request.get('continue', '/')
        continue_url = str(continue_url)
        logging.info('continue')
        logging.info(continue_url)
        if email:
            logging.info(email)
            logging.info(email)
            logging.info('redirecting')
            self.redirect(continue_url)
            return

        logging.info(self.request.query)
        self.redirect(users.create_login_url(self.request.path + '?continue=' + continue_url))

class LogoutHandler(APIHandler):
    def get(self):
        continue_url = self.request.get('continue', '/')
        self.redirect(users.create_logout_url(continue_url))

class WhoamiHandler(APIHandler):
    @staticmethod
    def get_buyer_id(email):
        return hashlib.sha256('asdkoiajsdoijasdojasdasdoijasod' + email).hexdigest()
    
    def get(self):
        email = self.check_authorization()
        if not email:
            self.dumps({'error': 'not logged in'})
            return

        key_string = 'Whoami/' + email
        ret = memcache.get(key_string)
        sandbox = SandboxHelper.is_sandbox(self)
        if not ret or ret.get('version', None) != 4 or sandbox:
            logging.info('grabbing fresh registration')
            ret = {'email': email, 'buyer_id': WhoamiHandler.get_buyer_id(email) }
            key = db.Key.from_path('User', email)
            registration = db.get(key)
            if registration is not None:
                ret['subscription_expiration'] = registration.subscription_expiration
                ret['registration_id'] = registration.registration_id
                ret['version_code'] = registration.version_code
            ret['version'] = 4
            current_user = users.get_current_user()
            if self.user:
                ret['nickname'] = self.user.nickname()
            if not sandbox:
                memcache.set(key_string, ret)
            #del ret['version']
        self.dumps(ret)

class BadgeHandler(APIHandler):
    def get(self):
        email = self.check_authorization()
        if email is None:
            return

        now = int(time.time() * 1000)
        # if no start is provided, return a default and the current time
        # do the same for an invalid after_date
        after_date = self.get_request_int_argument('after_date')
        if after_date is None or after_date > now:
            self.dumps({'email': email, 'badge': 0, 'date': now})
            return

        # see if we have the last incoming sms is before the provided after_date
        # and we can short circuit
        cur_last_incoming_sms = memcache.get('LastIncomingSms/' + email)
        if cur_last_incoming_sms is not None and cur_last_incoming_sms <= after_date:
            logging.info('memcache hit')
            self.dumps({'email': email, 'badge': 0, 'date': after_date, 'cur_last_incoming_sms': cur_last_incoming_sms})
            return

        logging.info('memcache miss')

        data = db.GqlQuery("SELECT * FROM Sms WHERE email=:1 and date > :2 order by date ASC", email, after_date)
        count = 0
        data = list(data)
        logging.info(data)
        logging.info('count: ' + str(count))

        last_incoming_sms = after_date
        # we can count on this to be returned in ascending order
        for sms in data:
            # badge for incoming only
            if sms.type == 1:
                count = count + 1
            last_incoming_sms = sms.date

        logging.info(last_incoming_sms)
        logging.info(cur_last_incoming_sms)
        if last_incoming_sms > cur_last_incoming_sms:
            logging.info('last_incoming_sms > cur_last_incoming_sms')
            memcache.set('LastIncomingSms/' + email, last_incoming_sms)
        else:
            logging.info('last_incoming_sms < cur_last_incoming_sms')

        self.dumps({'email': email, 'badge': count, 'date': now})

class DialHandler(APIHandler):
    def get(self):
        email = self.check_authorization()
        if email is None:
            return
        
        number = self.request.get('number', None)
        if number is None:
            self.dumps({'error': 'no number provided'})
            return

        PushHelper.push(email, { 'type': 'dial', 'email': email, 'number': number })
        self.dumps({'success': True})

class PhoneEventHandler(UserObjectHandler):
    registration = None
    expired = False

    def where(self):
        query = ''
        values = ()
        after_date = self.get_request_int_argument('after_date')
        before_date = self.get_request_int_argument('before_date')
        min_date = self.get_request_int_argument('min_date')
        max_date = self.get_request_int_argument('max_date')
        date = self.request.get('date', None)
        if min_date is not None:
            query += "AND date >= :%d " % (len(values) + 2)
            values += min_date,
        if max_date is not None:
            query += "AND date <= :%d " % (len(values) + 2)
            values += max_date,
        if after_date is not None:
            query += "AND date > :%d " % (len(values) + 2)
            values += after_date,
        if before_date is not None:
            query += "AND date < :%d " % (len(values) + 2)
            values += before_date,
        return (query, values)

    def should_put(self, email, envelope, put):
        sync = envelope.get('sync', None)
        if sync is None:
            # if we do not have a forward_web property set, that's just weird.
            # I just have this here, just in case, so shit works rather than doesn't work.
            return self.registration.forward_web is None or self.registration.forward_web == True
        else:
            return sync

    def should_process(self, email, envelope):
        key = db.Key.from_path('User', email)
        self.registration = db.get(key)
        if self.registration is None:
            logging.info('not registered')
            self.dumps_raw({ 'error': 'not registered', 'registered': False })
            return False

        registration = self.registration
        logging.info(registration.subscription_expiration)
        logging.info(time.time() * 1000)

        now = time.time() * 1000

        # valid expiration time
        if registration.subscription_expiration > now:
            return True

        # iOS users beta ends on ~feb 1, regardless if account was opened a while ago
        if registration.registration_id.startswith("ios:"):
            if registration.subscription_expiration < 1327109062435:
                registration.subscription_expiration = 1327109062435
                db.put(registration)

        if registration.subscription_expiration < now - 2 * 7 * 24 * 60 * 60 * 1000:
            self.dumps_raw({ 'error': 'expired' })
            return False

        # expired account, let's jack up the messages
        self.expired = True
        for event in envelope['data']:
            type = event.get('type', None)
            if not type:
                logging.info('no type provided?')
                logging.error(event)
                continue
            if type != self.get_notify_type() or event['number'] == 'DeskSMS' or not event.get('message', None):
                continue

            logging.info(event)
            event['message'] = event['message'][0:10] + "... " + " Your DeskSMS trial has expired! Please upgrade from within the DeskSMS Android application or at the https://desksms.appspot.com website!"

        return True

    def order_by(self):
        return "date ASC"

    def on_put(self, email, envelope, put):
        logging.info(email)

        try:
            version_code = envelope['version_code']
            logging.info(version_code)
            version_code = int(version_code)
        except:
            version_code = 0

        # we should have already received this in should_put
        registration = self.registration

        dirty_registration = False;

        if version_code != registration.version_code:
            registration.version_code = version_code
            dirty_registration = True

        registration_id = envelope.get('registration_id', None)

        if registration_id and registration.registration_id != registration_id:
            registration.registration_id = registration_id
            dirty_registration = True

        if not registration.subscription_expiration:
            registration.subscription_expiration = SubscriptionHelper.calculate_free_expiration_date(registration.registration_date)
            dirty_registration = True

        if dirty_registration:
            db.put_async(registration)

        initial_sync = envelope['is_initial_sync']
        all_events = envelope['data']
        # don't send notifications on initial sync or if there's just an odd number of messages.
        if initial_sync or len(all_events) > 20:
            logging.info('bailing out of push notifications:')
            logging.info(len(all_events))
            logging.info(initial_sync)
            return

        results = {}
        return_envelope = { 'data': results }

        # sort it into date increasing order before we send messages off.
        all_events = sorted(all_events, key=lambda event: event['date'])

        user_contacts_messages = {}

        for event in all_events:
            try:
                type = event.get('type', None)

                # only forward incoming messages
                if type != self.get_notify_type():
                    continue

                number = event['number']
                date = event['date']
                result = {'error': 'unknown error'}
                results['%s/%s' % (number, date)] = result

                message = event.get('message', None)
                if not message:
                    logging.error('message is none on event:')
                    logging.error(event)
                    continue

                subject = event['subject']
                name = event.get('name', None)
                has_desksms_contact = event.get('has_desksms_contact', False)

                if number is None or len(number) == 0:
                    logging.error('no number provided')
                    result['error'] = 'no number provided'
                    continue

                if message is None or len(message) == 0:
                    logging.error('no message provided')
                    result['error'] = 'no message provided'
                    continue

                entered_number = event.get('entered_number', None)

                # during incoming SMS, we need to resolve the number to an address
                # that the user is accustomed to contacting.
                # this is the number that they have entered into their phone, if it
                # exists. so, for the cleaned_number, let's prefer entered_number
                # if it exists.
                # let's hope this doesn't result in collisions...

                # now, when we send messages via chat and email,
                # we provide the send helpers with the user provided number
                # if it exists.
                if entered_number:
                    logging.info('preferring user provided number: ' + entered_number)
                    if not NumberHelper.are_similar(entered_number, number):
                        logging.error('entered number is not similar to actual number?')
                        logging.info(entered_number)
                        logging.info(number)
                        preferred_number = number
                        entered_number = None
                    else:
                        preferred_number = entered_number
                else:
                    preferred_number = number

                cleaned_number = NumberHelper.clean(preferred_number)

                # this cleaned number is then mapped to the actual number
                # as it is pulled directly from the SMS provider.
                key_string = email + '/' + cleaned_number
                last_message = UserContactLastMessage()
                last_message.last_message = message
                last_message.last_message_name = name
                # now that we have our "preferred" number, map it to the actual network number
                last_message.last_message_number = number
                user_contacts_messages[key_string] = last_message

                del result['error']

                # if we do not have a property set, that's just weird.
                # I just have this None check here, just in case, so shit works rather than doesn't work.
                if registration.forward_xmpp == True or registration.forward_xmpp is None or preferred_number == 'DeskSMS':
                    logging.info('xmpp')
                    try:
                        XMPPSendHelper.send(email, preferred_number, message, name, has_desksms_contact)
                        result['success_xmpp'] = True
                    except Exception, exception:
                        result['success_xmpp'] = False
                        logging.error('error during sending xmpp')
                        logging.error(email)
                        logging.error(number)
                        logging.error(message)
                        logging.error(name)
                        logging.error(exception)
                        logging.error(traceback.format_exc(exception))
                    logging.info('done xmpp')

                if ((registration.forward_email == True or registration.forward_email is None) and not self.expired) or preferred_number == 'DeskSMS':
                    logging.info('mail')
                    try:
                        mail_message = """
                        %s


                        ===========================
                        Please follow your response with two empty lines to ensure proper delivery!
                        Unsubsubscribe? Toggle the email setting in the DeskSMS application on your phone.
                        - DeskSMS
                        """ % (message)
                      
                        MailSendHelper.send(email, preferred_number, mail_message, name, subject)
                        result['success_email'] = True
                    except Exception, exception:
                        result['success_email'] = False
                        logging.error('error during sending mail')
                        logging.error(email)
                        logging.error(number)
                        logging.error(message)
                        logging.error(name)
                        logging.error(exception)
                        logging.error(traceback.format_exc(exception))
                    logging.info('done mail')

            except Exception, exception:
                logging.error('error processing incoming message')
                logging.error('email: ' + email)
                logging.error(traceback.format_exc(exception))



        # let's toss all these user contact messages into memcached
        # for xmpp
        if registration.forward_xmpp:
            logging.info('putting memcached messages')
            memcache.set_multi(user_contacts_messages, 300, 'UserContactLastMessage/')
        
        logging.info('putting user contact number mappings')
        user_contacts_to_put = []
        for key_string in user_contacts_messages:
            logging.info(key_string)
            user_contact = UserContact(key_name = key_string)
            last_sms = user_contacts_messages[key_string]
            number = last_sms.last_message_number
            user_contact.number = number
            user_contacts_to_put.append(user_contact)

        if len(user_contacts_to_put) > 0:
            if len(user_contacts_to_put) > 50:
                logging.error('large put?')
            logging.info('putting user contacts: %s' % len(user_contacts_to_put))
            db.put_async(user_contacts_to_put)

        registrations = envelope.get('registrations', None)
        if registrations:
            url_parts = self.request.path.split('/')
            bucket = url_parts[len(url_parts) - 1]
            bucket = urllib.unquote(bucket)

            del envelope['registrations']
            envelope_data = simplejson.dumps(envelope)
            if len(envelope_data) > 900:
                envelope_data = {}
            for device_registration in registrations:
                try:
                    PushHelper.push(email, { 'bucket': bucket, 'type': 'refresh', 'email': email, 'envelope': envelope_data }, registration_id = device_registration)
                except Exception, exception:
                    logging.error(traceback.format_exc(exception))

        # now, finally let's push to the extensions
        # should just always assume there is a push client available?
        # using memcached seems scary as then what if it gets purged before the timeout?
        if registration.forward_web != False:
            #success_web = False
            try:
                buyer_id = WhoamiHandler.get_buyer_id(email)
                logging.info('sending web push')
                push_data = { "tickle": True, "envelope": envelope }
                push_data = simplejson.dumps(push_data)
                post_data = {
                    'registration_id': buyer_id,
                    'data': push_data
                }
                post_data = urllib.urlencode(post_data)
                logging.info(post_data)
                rpc = urlfetch.create_rpc()
                urlfetch.make_fetch_call(rpc, 'http://n1.clockworkmod.com:9981/event', post_data, "POST")
            except Exception, exception:
                logging.error(traceback.format_exc(exception))

            #return_envelope['success_web'] = success_web
        else:
            logging.info('web push disabled')

        return return_envelope

class CallHandler(PhoneEventHandler):
    def get_object_type(self):
        return Call

    def new(self, email, data):
        return Call(key_name = '%s/%s/%d' % (email, data['number'], data['date']))

    def get_notify_type(self):
        return 'missed'

class SmsHandler(PhoneEventHandler):
    def get_object_type(self):
        return Sms

    @staticmethod
    def newSms(email, number, date):
        return Sms(key_name = '%s/%s/%d' % (email, number, date))

    def new(self, email, data):
        return SmsHandler.newSms(email, data['number'], data['date'])

    def get_notify_type(self):
        return 'incoming'

    def query(self, email):
        # see if we can optimize out this after_date query with memcache
        after_date = self.get_request_int_argument('after_date')
        before_date = self.get_request_int_argument('before_date')
        max_date = self.get_request_int_argument('max_date')
        if after_date is None or before_date is not None or max_date is not None:
            return PhoneEventHandler.query(self, email)

        # see if the last incoming sms is before the provided after_date
        # and we can short circuit
        cur_last_incoming_sms = memcache.get('LastIncomingSms/' + email)
        if cur_last_incoming_sms is not None and cur_last_incoming_sms <= after_date:
            logging.info('memcache hit')
            logging.info(cur_last_incoming_sms)
            return []

        ret = PhoneEventHandler.query(self, email)

        last_incoming_sms = after_date
        # we can count on this to be returned in ascending order
        for sms in ret:
            # we use all sms, because we want outbox updates to be retrievable
            # as well
            last_incoming_sms = sms.date

        if last_incoming_sms > cur_last_incoming_sms:
            memcache.set('LastIncomingSms/' + email, last_incoming_sms)
        else:
            logging.info('last_incoming_sms < cur_last_incoming_sms')
            logging.info(last_incoming_sms)
            logging.info(cur_last_incoming_sms)

        return ret

    def on_put(self, email, envelope, put):
        logging.info('clearing memcache')
        memcache.delete('LastIncomingSms/' + email, 10)
        return PhoneEventHandler.on_put(self, email, envelope, put)

class OutboxHandler(UserObjectHandler):
    def where(self):
        query = ''
        values = ()
        min_date = self.get_request_int_argument('min_date')
        max_date = self.get_request_int_argument('max_date')
        number = self.request.get('number', None)
        if min_date is not None:
            query += "AND date >= :%d " % (len(values) + 2)
            values += min_date,
        if max_date is not None:
            query += "AND date <= :%d " % (len(values) + 2)
            values += max_date,
        if number is not None:
            query += "AND number = :%d " % (len(values) + 2)
            values += number,
        return (query, values)

    @staticmethod
    def push_pending_outbox(email, for_sms = []):
        key = db.Key.from_path('User', email)
        registration = db.get(key)
        if registration is None:
            return (False, None)

        data = db.GqlQuery("SELECT * FROM OutboxSms WHERE email=:1 ORDER BY date ASC", email)
        #data = list(data)

        # what happens when the message queue gets enormous cause the phone is off or something?

        # there seems to be an eventual consistency issue here.
        # doing a read then a write for the same element seems to fail and return nothing.
        # This results in an empty push.
        found_sms = {}

        outbox = []
        for outgoing_sms in data:
            outbox_sms = {'number': outgoing_sms.number, 'message': outgoing_sms.message, 'date': outgoing_sms.date}
            outbox.append(outbox_sms)
            found_sms[outgoing_sms.key().name()] = outgoing_sms

        for sms in for_sms:
            if not found_sms.get(sms.key().name(), None):
                outbox_sms = {'number': sms.number, 'message': sms.message, 'date': sms.date}
                logging.info('fixed eventual consistency problem')
                logging.info(sms.key().name())
                outbox.append(outbox_sms)

        # c2dm can not handle structured data, so json string it.
        data = simplejson.dumps(outbox)

        logging.info(data)

        if len(data) > 900 or registration.registration_id.startswith('ios:'):
            # if the data is too long (1024 max), ask the client to poll by not sending outbox data
            return PushHelper.push(email, { 'type': 'outbox', 'email': email }, registration = registration)
        else:
            return PushHelper.push(email, { 'outbox': data, 'type': 'outbox', 'email': email }, registration = registration)

    def should_put(self, email, envelope, put):
        put_copy = list(put)
        # also store the messages in the sms bucket as pending
        for d in put_copy:
            try:
                o = SmsHandler.newSms(email, d.number, d.date)
                o.message = d.message
                o.email = email
                o.type = 3
                o.date = d.date
                o.number = d.number
                put.append(o)
            except Exception, exception:
                logging.error(exception)
                logging.error(traceback.format_exc(exception))
        return True

    def on_put(self, email, envelope, put):
        outgoing = [o for o in put if isinstance(o, OutboxSms)]
        push_result = OutboxHandler.push_pending_outbox(email, outgoing)
        ret = {}
        if push_result[0]:
            ret['success'] = True
        else:
            ret['error'] = push_result[1]
        data = []
        for sms in outgoing:
            data.append(sms.date)
        ret['data'] = data
        return ret;

    def get_object_type(self):
        return OutboxSms
    
    @staticmethod
    def newOutboxSms(email, number):
        date = int(time.time() * 1000)
        return OutboxSms(key_name = '%s/%s/%d' % (email, number, date), date = date, email = email)
        
    def new(self, email, data):
        return OutboxHandler.newOutboxSms(email, data['number'])

    def order_by(self):
        return "date ASC"

class PushHelper(object):
    google_auth = None
    
    @staticmethod
    def retrieveGoogleAuth():
        if PushHelper.google_auth is not None:
           return PushHelper.google_auth

        url = "https://www.google.com/accounts/ClientLogin"
        data = {
            "accountType": "HOSTED_OR_GOOGLE",
            "Email": "____YOUR_EMAIL____",
            "Passwd": "____YOUR_PASSWORD____",
            "source": "koush-desktopsms",
            "service": "ac2dm"
        }

        data = urllib.urlencode(data)

        f = urllib2.urlopen(url, data)
        lines = f.read()

        lines = lines.split('\n')
        for line in lines:
            if line.startswith('Auth'):
                parts = line.split('=')
                PushHelper.google_auth = parts[1]
                return PushHelper.google_auth

        return None

    push_results = {
        'QuotaExceeded': 'DeskSMS quota exceeded. Please try again later.',
        'DeviceQuotaExceeded': 'Your push notification quota has been exceeded.',
        'InvalidRegistration': 'Your device could not be contacted. Please try relogging into the DeskSMS Android application.',
        'NotRegistered': 'Your device could not be contacted. Please try relogging into the DeskSMS Android application.',
        'MessageTooBig': 'Your message was too long.',
        'MissingCollapseKey': 'DeskSMS server error (collapse key).'
    }

    @staticmethod
    def push(email_or_resource, data, collapse_key = None, registration = None, async = False, registration_id = None):
        email = email_or_resource.split('/')[0].lower()
        logging.info('attempting to push to %s' % (email))

        if registration_id is None:
            if registration is None:
                key = db.Key.from_path('User', email)
                registration = db.get(key)

            if registration is None:
                raise Exception('registration is unavailable')

            registration_id = registration.registration_id
        
        if registration_id.startswith("ios"):
          parts = registration_id.split(':')
          client = parts[1]
          url = 'http://n1.clockworkmod.com:9981/apn/' + urllib.quote(client)
          post_data = {
            'data': urllib.urlencode(data)
          }
          post_data = urllib.urlencode(post_data)
          logging.info(post_data)

          if not async:
            logging.info('sync request')
            try:
                try:
                    req = urllib2.Request(url)
                    f = urllib2.urlopen(req, post_data)
                    ret = f.read()
                    logging.info(ret)
                    data = simplejson.loads(ret)
                    if data.get('error', None):
                        return (False, data.get('error', None))
                    return (True, None)
                except urllib2.HTTPError, error:
                    ret = error.read()
                    logging.info(ret)
                    data = simplejson.loads(ret)
                    return (False, data.get('response_message', 'Unknown HTTP error.'))
            except Exception, exception:
                logging.error(exception)
                logging.error(traceback.format_exc(exception))
                return (False, 'Parse error.')

          else:
            # this currently has no error handling, so always do it async
            logging.info('async request')
            rpc = urlfetch.create_rpc()
            urlfetch.make_fetch_call(rpc, url, post_data, "POST")
            return (True, 'success')

        elif registration_id.startswith('gcm:'):
            parts = registration_id.split(':')
            registration_id = parts[1]
            if collapse_key is None:
                collapse_key = str(int(time.time()))

            url = "https://android.googleapis.com/gcm/send"
            post_data = {
                'collapse_key': collapse_key,
                'registration_ids': [ registration_id ]
            }

            post_data['data'] = data
            post_data = simplejson.dumps(post_data)
            logging.info(post_data)

            if not async:
                logging.info('sync request')
                req = urllib2.Request(url)
                req.add_header('Authorization', 'key=____YOUR___AUTH_____')
                req.add_header('Content-Type', 'application/json')
                try:
                    try:
                        f = urllib2.urlopen(req, post_data)
                        result = f.read()
                    except urllib2.HTTPError, error:
                        result = error.read()

                    logging.info('push result: ' + result)
                    json_result = simplejson.loads(result)
                    
                    return (json_result.get('success', 0) == 1, result)
                except Exception, exception:
                    logging.error(exception)
                    logging.error(traceback.format_exc(exception))
                    PushHelper.google_auth = None
                    return (False, 'Push service error.')

            else:
                headers = {
                    'Authorization': 'key=____YOUR___AUTH_____',
                    'Content-Type': 'application/json'
                }
                logging.info('async request')
                rpc = urlfetch.create_rpc()
                urlfetch.make_fetch_call(rpc, url, post_data, "POST", headers)
                return rpc
        else:
            auth = PushHelper.retrieveGoogleAuth()
            auth_header =  'GoogleLogin auth=' + auth
            if auth is None:
                raise Exception('google_auth is unavailable')

            if collapse_key is None:
                collapse_key = str(int(time.time()))

            url = "http://android.apis.google.com/c2dm/send"
            post_data = {
                'collapse_key': collapse_key,
                'registration_id': registration_id
            }

            headers = {
                'Authorization': 'GoogleLogin auth=' + auth
            }

            for d in data:
                post_data['data.%s' % (d)] = str(data[d]).encode('utf-8')

            post_data = urllib.urlencode(post_data)
            logging.info(post_data)
            
            if not async:
                logging.info('sync request')
                req = urllib2.Request(url)
                req.add_header('Authorization', auth_header)
                try:
                    try:
                        f = urllib2.urlopen(req, post_data)
                        result = f.read()
                    except urllib2.HTTPError, error:
                        result = error.read()

                    logging.info('push result: ' + result)
                    pairs = result.split('=')
                    if pairs[0] == "id":
                        return (True, pairs[1])

                    push_error = PushHelper.push_results.get(pairs[1])
                    if not push_error:
                        push_error = "DeskSMS Server error."

                    logging.error(push_error)
                    return (False, push_error)
                except Exception, exception:
                    logging.error(exception)
                    logging.error(traceback.format_exc(exception))
                    PushHelper.google_auth = None
                    return (False, 'Push service error.')

            else:
                logging.info('async request')
                rpc = urlfetch.create_rpc()
                urlfetch.make_fetch_call(rpc, url, post_data, "POST", { 'Authorization': auth_header })
                return rpc
    
class ChatHandler(webapp2.RequestHandler):
    def handle(self):
        sender = self.request.get('from')
        to =  self.request.get('to')
        body = self.request.get('body')
        
        logging.info(sender)
        
        body = body.strip()
        if len(body) == 0:
            logging.info('empty body')
            return

        try:
            email = sender.split('/')[0].lower()
            key = db.Key.from_path('User', email)
            registration = db.get(key)
            if registration is None:
                logging.error('registration is unavailable')
                return

            logging.info('chat message from %s to %s: %s' % (sender, to, body))
            cleaned_number = to.split('@')[0]

            key = db.Key.from_path('UserContact', email + '/' + cleaned_number)
            user_contact = db.get(key)
            if user_contact and user_contact.number and len(user_contact.number) > 0:
                if not NumberHelper.are_similar(cleaned_number, user_contact.number):
                    logging.error('chat: entered number is not similar to actual number?')
                    logging.info(user_contact.number)
                    logging.info(cleaned_number)
                    number = cleaned_number
                    user_contact.number = None
                    db.put_async(user_contact)
                else:
                    number = user_contact.number
            else:
                # this is first contact
                number = cleaned_number

            outbox_sms = OutboxHandler.newOutboxSms(email, number)
            outbox_sms.message = body[:500]
            outbox_sms.number = number
            db.put_async(outbox_sms)
            push_result = OutboxHandler.push_pending_outbox(email, [outbox_sms])

            if push_result[0]:
                return

            XMPPSendHelper.send(email, number, "Error sending message: " + push_result[1], "DeskSMS")

        except Exception, exception:
            logging.error('error during incoming chat message')
            logging.error(exception)
            logging.error('sender: ' + sender)
            logging.error('to: ' + to)
            logging.error('body: ' + body)
            raise
              
    def get(self):
        self.handle()

    def post(self):
        self.handle()

class MailHandler(InboundMailHandler):
    # apparently python can't handle 8-bit body decoding.
    # this is the hacky workaround I found that turns it into 7-bit.
    # http://code.google.com/p/googleappengine/issues/detail?id=2383
    @staticmethod
    def fixup_body_encoding(body):
        try:
            if isinstance(body, EncodedPayload):
                logging.info('encoding:')
                logging.info(body.encoding)
                if body.encoding == '8bit':
                    body.encoding = '7bit' 
                    logging.info('Body encoding fixed')
        except:
            logging.error('failed to decode body')

    def receive(self, mail_message):
        try:
            plaintext_bodies = mail_message.bodies('text/plain')
            html_bodies = mail_message.bodies('text/html')

            #logging.info('html')
            #for content_type, body in html_bodies:
            #    decoded_html = body.decode()
            #    logging.info(decoded_html)

            logging.info('text bodies')
            message = None
            for content_type, body in plaintext_bodies:
                MailHandler.fixup_body_encoding(body)
                decoded_text = body.decode()
                logging.info(decoded_text)
                if not message:
                    message = decoded_text

            if not message:
                logging.error('no plaintext body found')
                return

            # at some point app engine/gmail or something switched to crlf instead
            # of just lf.
            message = message.replace('\r\n', '\n');
            message = message.split('\n\n')[0]
            if len(message) > 700:
                logging.error('message too long? continuing to see what happens...')

            message = message.strip()
            if len(message) == 0:
                logging.error('empty message')
                return

            logging.info('sending: ' + message)

            match = re.compile('<(.*?)>').search(mail_message.sender)
            if match is None:
                email = mail_message.sender
            else:
                email = match.groups()[0]
            logging.info('email: '+ email)

            key = db.Key.from_path('User', email)
            registration = db.get(key)
            if registration is None:
                logging.error('no registration found')
                return

            logging.info(self.request.url)
            url_parts = self.request.url.split('/')
            encoded_email = url_parts[len(url_parts) - 1]
            decoded_email = urllib.unquote(encoded_email)
            cleaned_number = decoded_email.split('@')[0]

            key = db.Key.from_path('UserContact', email + '/' + cleaned_number)
            user_contact = db.get(key)
            if user_contact and user_contact.number and len(user_contact.number) > 0:
                if not NumberHelper.are_similar(cleaned_number, user_contact.number):
                    logging.error('mail: entered number is not similar to actual number?')
                    logging.info(user_contact.number)
                    logging.info(cleaned_number)
                    number = cleaned_number
                    user_contact.number = None
                    db.put_async(user_contact)
                else:
                    number = user_contact.number
            else:
                # this is first contact
                number = cleaned_number

            logging.info(number)

            outbox_sms = OutboxHandler.newOutboxSms(email, number)
            outbox_sms.message = message[:500]
            outbox_sms.number = number
            date = int(time.time() * 1000)
            outbox_sms.date = date
            db.put_async(outbox_sms)
            OutboxHandler.push_pending_outbox(email, [outbox_sms])

        except Exception, exception:
            logging.error('error during incoming mail')
            logging.error('sender: ' + mail_message.sender)
            logging.error('recipient: ' + mail_message.to)
            logging.error(exception)
            raise

class SettingsHandler(APIHandler):
    def get(self):
        if self.request.query_string and self.request.query_string != '':
            self.post()
            return

        email = self.check_authorization()
        if email is None:
            return

        key = db.Key.from_path('User', email)
        registration = db.get(key)
        if registration is None:
            logging.info('no registration found')
            self.dumps({'error': 'not registered'})
            return

        logging.info(registration)
        logging.info(registration.key().name())
        logging.info(registration.forward_email)
        self.dumps({'forward_email': registration.forward_email != False, 'forward_xmpp': registration.forward_xmpp != False, 'forward_web': registration.forward_web != False})

    def post(self):
        email = self.check_authorization()
        if email is None:
            return

        key = db.Key.from_path('User', email)
        registration = db.get(key)
        if registration is None:
            logging.info('no registration found')
            self.dumps({'error': 'not registered'})
            return

        lambdas = {
            bool: lambda x: x != 'false'
        }

        all_settings = {
            'forward_web': lambdas[bool],
            'forward_xmpp': lambdas[bool],
            'forward_email': lambdas[bool]
        }

        for setting in all_settings:
            val = self.request.get(setting, None)
            if val is not None:
                val = all_settings[setting](val)
                setattr(registration, setting, val)
        db.put_async(registration)

        ret = {}
        for setting in all_settings:
            ret[setting] = getattr(registration, setting)

        self.dumps(ret)

        ret['type'] = 'settings'
        if self.request.get('tickle', None) == 'true':
          PushHelper.push(email, ret)

class SubscribedHandler(webapp2.RequestHandler):
    def handle(self):
        for arg in self.request.arguments():
            logging.info("%s: %s" % (arg, self.request.get(arg)))

        sender = self.request.get('from')
        logging.info('%s just subscribed' % (sender))
        email = sender.split('/')[0].lower()
        cleaned_number = self.request.get('to').split('@')[0]

        key_string = email + '/' + cleaned_number
        key = db.Key.from_path('UserContactSubscribed', key_string)
        user_contact_subscribed = db.get(key)

        # check if there are any pending sms that need to be sent via xmpp
        if not user_contact_subscribed:
            last_message = memcache.get('UserContactLastMessage/' + key_string)
            if last_message:
                logging.info('sending post subscription message')
                message = last_message.last_message
                name = last_message.last_message_name
                # do not correspond from usercontact.number,
                # as that is not the account the user subscribed to!
                # use "cleaned_number" which is the subscribed address,
                # and the user "preferred" number.
                number = cleaned_number
                user_contact_subscribed = UserContactSubscribed(key_name = key_string)
                db.put_async(user_contact_subscribed)
                XMPPSendHelper.send(email, number, message, name)
            else:
                logging.info('no pending message found in memcached')
        
        #self.response.out.write(simplejson.dumps({'success': True}))

    def get(self):
        self.handle()

    def post(self):
        self.handle()


from xml.etree import ElementTree

class XMPPSendHelper(object):
    @staticmethod
    def send(email, number, message, name, has_desksms_contact = False):
        cleaned_number = NumberHelper.clean(number)
        fromJid = '%s@desksms.appspotchat.com' % (cleaned_number)

        logging.info(email)
        logging.info(fromJid)

        # clean the prepended + on numbers
        xmpp.send_invite(email, fromJid)
        logging.info('attempting to send xmpp message from %s to %s' % (fromJid, email))
        if not name:
            name = number

        if name:
            # TODO: figure out why some IM clients dont seem to respect the
            # name associated with the contact...
            if False and has_desksms_contact:
                msg = "(DeskSMS) %s" % (message)
            else:
                msg = "(%s) %s" % (name, message)
        else:
            msg = message

        nick = ElementTree.Element('nick', attrib = { 'xmlns': 'http://jabber.org/protocol/nick' })
        nick.text = name

        body = ElementTree.Element('body', attrib = { 'from': fromJid + '/bot', 'to': email, 'type': 'chat' })
        body.text = msg

        raw_xml = ElementTree.tostring(body) + ElementTree.tostring(nick)

        logging.info(raw_xml)

        status_code = xmpp.send_message(email, raw_xml, fromJid, raw_xml = True)
        # this is weirding out some chat clients.
        #xmpp.send_presence(email, status = name, from_jid = fromJid)
        ret = (status_code == xmpp.NO_ERROR)
        logging.info('result: ' + str(ret))
        return ret

class MailSendHelper(object):
    @staticmethod
    def send(email, number, message, name, subject = None):
        if number:
            if name is None:
                name = number
            cleaned_number = NumberHelper.clean(number)
            sender = '%s (DesktopSMS) <%s@desksms.appspotmail.com>' % (name, cleaned_number)
            # comma is apparently not a valid character in sender addresses on app engine?
            sender = sender.replace(',', '')
        else:
            sender = "DeskSMS <noreply@desksms.appspotmail.com>"
        
        if subject is None:
            raise Exception("No subject provided for message")
        if message is None:
            raise Exception("No message provided for message")

        logging.info(sender)
        smail = mail.EmailMessage(sender = sender, subject = subject)

        smail.to = email
        smail.body = message
        
        smail.send()

class NumberHelper(object):
    @staticmethod
    def strip(number):
        cleaned_number = ""
        for c in number:
            if c >= '0' and c <= '9':
                cleaned_number += c
        return str(int(cleaned_number))

    @staticmethod
    def are_similar(n1, n2):
        try:
            if n1 == n2:
                return True
            if n1 is None or n2 is None:
                return False
            cn1 = NumberHelper.strip(n1)
            cn2 = NumberHelper.strip(n2)
            if len(cn1) < 7 or len(cn2) < 7:
                return False
            if cn1 in cn2 or cn2 in cn1:
                return True
            return False
        except Exception, exception:
            logging.error(exception)
            logging.error(traceback.format_exc(exception))
            return False

    @staticmethod
    def clean(number):
        # number can be 'NO REPLY' and other stuff for blocked numbers...
        return number.replace('+', '').replace(' ', '-')

class TickleHandler(webapp2.RequestHandler):
    def handle(self):
        current_user = users.get_current_user()
        if not users.is_current_user_admin():
            self.dumps(simplejson.dumps({'error': 'not administrator'}))
            return

        url_parts = self.request.path.split('/')
        encoded_email = url_parts[len(url_parts) - 1]
        decoded_email = urllib.unquote(encoded_email)

        data = {}
        for arg in self.request.arguments():
            if arg.startswith('data.'):
                data[arg[5:]] = self.request.get(arg)

        logging.info(data)
        self.response.out.write(PushHelper.push(decoded_email, data))

    def get(self):
        self.handle()

    def post(self):
        self.handle()


class PushHandler(APIHandler):
    def handle(self):
        email = self.check_authorization()
        if not email:
            self.dumps({'error': True})
            return

        type = self.request.get('type', None)

        if not type:
            self.dumps({'error': 'no type provided'})
            return

        data = { 'type': type }
        for arg in self.request.arguments():
            if arg.startswith('data.'):
                data[arg[5:]] = self.request.get(arg)

        logging.info(data)
        result = PushHelper.push(email, data, registration_id=self.request.get('registration', None))
        if result[0]:
            self.dumps({'success': True})
        else:
            self.dumps({'error': result[1]})

    def get(self):
        self.handle()

    def post(self):
        self.handle()


class SubscriptionHelper(object):
    @staticmethod
    def calculate_free_expiration_date(registration_date):
        if not registration_date:
            registration_date = int(time.time() * 1000)

        return max(registration_date + (14 * 24 * 60 * 60 * 1000), 1314331991822)

class RegisterHandler(webapp2.RequestHandler):
    def handle(self):
        current_user = users.get_current_user()
        if not current_user:
            logging.info('current_user is none')
            logging.info(self.request.body)
            logging.info(self.request.headers)
            self.response.set_status(500)
            return
        email = current_user.email().lower()
        registration_id = self.request.get('registration_id')
        send_email = self.request.get('send_email', "false") == "true"

        try:
            version_code = self.request.get('version_code')
            logging.info(version_code)
            version_code = int(version_code)
        except:
            version_code = 0

        key = db.Key.from_path('User', email)
        user = db.get(key)
        if not user:
            # 10 day free subscription
            registration_date = int(time.time() * 1000)
            subscription_expiration = SubscriptionHelper.calculate_free_expiration_date(registration_date)
            user = User(key_name = email, subscription_expiration = subscription_expiration, forward_xmpp = True, forward_email = False, forward_web = True, registration_date = registration_date)
            send_email = True

            # only process referrals for new signups
            try:
                key = db.Key.from_path('Referral', email)
                existing_referral = db.get(key)
                if existing_referral:
                    now = int(time.time()) * 1000
                    existing_referral.referral_date = now
                    db.put(existing_referral)
                    sandbox = SandboxHelper.is_sandbox(self)
                    referrer = existing_referral.referrer
                    StatusHandler.update_status(referrer, sandbox)

                    MailSendHelper.send(referrer, None, "You have received 10 days of free time for referring %s!" % (email), None, "DeskSMS Referral")

            except Exception, exception:
                logging.error(exception)
                logging.error(traceback.format_exc(exception))  



        if send_email:
            try:
                message = """
                Welcome to DeskSMS! Below you will find links to the DeskSMS website, free browser extensions,
                and other tips to make sending texts even easier!

                Chrome Extension - http://bit.ly/mPlZuy
                FireFox Extension - http://bit.ly/owfySU
                DeskSMS Web Interface - https://desksms.appspot.com

                Did you know DeskSMS has a handy Android Home Screen Widget to control your notification settings? Check it out!
                Check out these email filter tips to prevent excessive phone notifications: http://bit.ly/Wg2tE2

                I'd love to hear about any praise (or complaints/bugs) about DeskSMS! Drop me a line at koush@clockworkmod.com!

                - Koush, DeskSMS Developer
                """

                MailSendHelper.send(email, None, message, None, "Welcome to DeskSMS!")
            except Exception, exception:
                logging.error(exception)
                logging.error(traceback.format_exc(exception))

        user.registration_id = registration_id
        user.version_code = version_code
        db.put(user)
        ret = memcache.delete('Whoami/' + email, 10)

        self.response.out.write(simplejson.dumps({'registration_id': registration_id, 'email': email}))
        
    def get(self):
        self.handle()
    
    def post(self):
        self.handle()

class MainHandler(webapp2.RequestHandler):
    def get(self):
        self.response.out.write('<a href="%s">logout</a>' % (users.create_logout_url("/")))

class ProxyHandler(APIHandler):
    def get(self):
        try:
            proxied = self.request.get('proxied', None)
            alt = self.request.get('alt', None)
            if not proxied:
                return;
            f = urllib2.urlopen(proxied)
            info = f.info()
            self.response.headers['Content-Type'] = info['Content-Type']
            self.response.headers['Access-Control-Allow-Origin'] = '*'
            data = f.read()
            if alt == 'json':
                self.dumps({ 'data': base64.b64encode(data) })
            else:
                self.response.out.write(data)
        except:
            self.response.set_status(500)
            pass

class PushPingHandler(webapp2.RequestHandler):
    def get(self):
        try:
            f = urllib2.urlopen('http://n1.clockworkmod.com:9981/ping?nonce=' + str(time.time()))
            self.response.headers['Cache-Control'] = "max-age=0, no-cache, no-store, must-revalidate, post-check=0, pre-check=0"
            ret = f.read()
            self.response.out.write(ret)
        except Exception, exception:
            logging.error(exception)
            logging.error(traceback.format_exc(exception))
            self.response.out.write('{"error": true}')

class PingHandler(APIHandler):
    def get(self):
        email = self.check_authorization(False)
        if not email:
            self.dumps({'error': True})
            return

        key = db.Key.from_path('User', email)
        registration = db.get(key)
        if not registration:
            self.dumps({'error': True})
            return

        PushHelper.push(email, { 'type': 'ping' })

class PongHandler(APIHandler):
    def get(self):
        email = self.check_authorization()
        if not email:
            self.dumps({'error': True})
            return

        key = db.Key.from_path('User', email)
        registration = db.get(key)
        if not registration:
            self.dumps({'error': True})
            return

        ret = PushHelper.push(email, { 'type': 'pong' })
        if ret[0]:
          self.dumps({"success": True})
        else:
          self.dumps({"error": ret[1]})

class SimpleTickleHandler(APIHandler):
    def get(self):
        email = self.check_authorization()
        if not email:
            self.dumps({'error': True})
            return

        type = self.request.get('type', None)

        if not type:
            self.dumps({'error': 'no type provided'})
            return

        key = db.Key.from_path('User', email)
        registration = db.get(key)
        if not registration:
            self.dumps({'error': True})
            return

        result = PushHelper.push(email, { 'type': type })
        if result[0]:
            self.dumps({'success': True})
        else:
            self.dumps({'error': result[1]})

class PurgeHandler(webapp2.RequestHandler):
    def get(self):
        # 2 weeks
        # logging.info('purging old Sms')
        # before_date = int(time.time() * 1000 - 14 * 24 * 60 * 60 * 1000)
        # data = db.GqlQuery("SELECT __key__ FROM Sms WHERE date < :1 limit 1000", before_date)
        # db.delete(list(data))

        # 5 minutes
        logging.info('purging old Outbox')
        before_date = int(time.time() * 1000 - 5 * 60 * 1000)
        data = db.GqlQuery("SELECT __key__ FROM OutboxSms WHERE date < :1 limit 1000", before_date)
        db.delete_async(list(data))

        outboxes = {}
        data = db.GqlQuery("SELECT * FROM OutboxSms where date > :1 order by date ASC", before_date)
        data = list(data)
        for outgoing_sms in data:
            outbox = outboxes.get(outgoing_sms.email)
            if not outbox:
                outbox = []
                outboxes[outgoing_sms.email] = outbox
            
            outbox_sms = {'number': outgoing_sms.number, 'message': outgoing_sms.message, 'date': outgoing_sms.date}
            outbox.append(outbox_sms)
        
        for email in outboxes:
            outbox = outboxes[email]
            data = simplejson.dumps(outbox)
            rpc = PushHelper.push(email, { 'outbox': data, 'type': 'outbox', 'email': email }, async = True)
        # 2 weeks
        # logging.info('purging old Calls')
        # before_date = int(time.time() * 1000 - 14 * 24 * 60 * 60 * 1000)
        # data = db.GqlQuery("SELECT __key__ FROM Call WHERE date < :1 limit 1000", before_date)
        # db.delete(list(data))

class SandboxHelper(object):
    @staticmethod
    def is_sandbox(handler):
        return "2.desksms.appspot.com" in handler.request.url

class StatusHandler(APIHandler):
    @staticmethod
    def update_status(email, sandbox):
        key = db.Key.from_path('User', email)
        registration = db.get(key)
        if registration is None:
            logging.info('no registration found')
            return {'error': 'not registered'}
      
        buyer_id = WhoamiHandler.get_buyer_id(email)
      
        needs_put = False
        logging.info('sandbox')
        logging.info(sandbox)
        # convert this to a string for the querystring
        if sandbox:
            sandbox = 'true'
        else:
            sandbox = 'false'

        f = urllib2.urlopen('https://clockworkbilling.appspot.com/api/v1/purchase/koushd@gmail.com/%s?sandbox=%s&nonce=%s' % (buyer_id, sandbox, time.time()))
        data = f.read()
        logging.info(data)
        # load the payload
        payload = simplejson.loads(data)
        # we are actually interested in the signed data, so grab that as the real payload.
        payload = simplejson.loads(payload['signed_data'])
      
        if not registration.registration_date:
            needs_put = True
            registration.registration_date = int(time.time() * 1000)
      
        subscription_expiration = SubscriptionHelper.calculate_free_expiration_date(registration.registration_date)
        logging.info('base expiration')
        logging.info(subscription_expiration)
        orders = payload['orders']
        for order in orders:
            order_date = order['order_date']
            order_subscription_length = 0
            if order['product_id'] == 'desksms.subscription0':
                order_subscription_length = 365

            if order['product_id'] == 'desksms.subscription1':
                order_subscription_length = 365

            if order['product_id'] == 'desksms.freebie':
                order_subscription_length = 28

            order_subscription_length = order_subscription_length * 24 * 60 * 60 * 1000
            expiration_date_from_order = order_date + order_subscription_length
            expiration_date_from_previous = subscription_expiration + order_subscription_length
          
            subscription_expiration = max(expiration_date_from_previous, expiration_date_from_order)
      
        data = db.GqlQuery("SELECT * FROM Referral WHERE referrer=:1", email)
        data = list(data)

        count = 0
        referral_length = 10 * 24 * 60 * 60 * 1000
        for referral in data:
            if count >= 5:
                break
            if referral.referral_date:
                count = count + 1
                expiration_date_from_referral = referral.referral_date + referral_length
                expiration_date_from_previous = subscription_expiration + referral_length
                subscription_expiration = max(expiration_date_from_previous, expiration_date_from_referral)

        now = int(time.time()) * 1000
        # if the subscription is expired and the user hasn't used their tabletsms promotion yet
        # let's give it to them
        if subscription_expiration < now and registration.promotion != 'tabletsms' and now < 1349138358000:
            registration.promotion_date = now
            registration.promotion = 'tabletsms'
            needs_put = True

        if registration.promotion == 'tabletsms':
            subscription_expiration = max(registration.promotion_date + 14 * 24 * 60 * 60 * 1000, subscription_expiration)

        if subscription_expiration != registration.subscription_expiration:
            needs_put = True
            registration.subscription_expiration = subscription_expiration

        if needs_put:
            db.put(registration)
            key_string = 'Whoami/' + email
            memcache.delete(key_string)
      
        ret = {
            'email': email,
            'buyer_id': buyer_id,
            'subscription_expiration': subscription_expiration,
            'purchases': payload,
            'registration_date': registration.registration_date,
            'registration_id': registration.registration_id,
            'version_code': registration.version_code,
            'forward_xmpp': registration.forward_xmpp,
            'forward_web': registration.forward_web,
            'forward_email': registration.forward_email,
        }
      
        return ret
      

    def get(self):
        email = self.check_authorization()
        if email is None:
            return
        
        sandbox = SandboxHelper.is_sandbox(self)
        ret = StatusHandler.update_status(email, sandbox)
        
        self.dumps(ret)

class ReadHandler(APIHandler):
    def get(self):
        email = self.check_authorization()
        if email is None:
            return

        PushHelper.push(email, { 'type': 'read' }, 'read', async = True)
        self.dumps({'success': True})

class DeleteConversationHandler(APIHandler):
    def get(self):
        email = self.check_authorization()
        if email is None:
            return
        
        number = self.request.get('number', None)
        if not number:
            self.dumps({'error': 'missing number'})
            return
        
        dates = self.request.get('dates', None)
        if not dates:
            self.dumps({'error': 'missing dates'})
            return

        dates = simplejson.loads(dates)
        
        keys = []
        for date in dates:
            key_string = '%s/%s/%d' % (email, number, date)
            key = db.Key.from_path('Sms', key_string)
            keys.append(key)

        db.delete(keys)
        self.dumps({'success': True})

class MmsImageHandler(APIHandler):
    def get(self):
        email = self.check_authorization()
        if email is None:
            return

        splits = self.request.path.split('/')
        number = urllib.unquote(splits[6])
        date = urllib.unquote(splits[7])

        logging.info(number)
        logging.info(date)

        key_string = '%s/%s/%s' % (email, number, date)
        key = db.Key.from_path('Sms', key_string)

        sms = db.get(key)
        if not sms or not sms.image:
            self.response.set_status(404)
            return

        self.response.headers['Cache-Control'] = 'max-age=31556926'
        self.response.headers['Content-Type'] = 'image/png'
        self.response.out.write(sms.image)

class ReferralHandler(APIHandler):
    def get(self):
        self.handle()
    
    def post(self):
        self.handle()

    def handle(self):
        email = self.check_authorization()
        if email is None:
            return

        logging.info(email)
        try:
            personal_message = self.request.get('message', 'DeskSMS for Android is an awesome app!')
            referral = self.request.get('referral')

            key = db.Key.from_path('User', referral)
            existing_user = db.get(key)
            if existing_user:
                self.dumps({"error": "That user is already signed up!"})
                return
                
            message = mail.EmailMessage(sender = email,
                                        subject = "DeskSMS for Android")
            message.to = referral

            referral_link = "https://desksms.appspot.com/refer?referrer=%s&referral=%s" % (email, referral)

            message.body = """
            %s

            %s
            """ % (personal_message, referral_link)

            message.html = """
            <html><head></head><body>
            %s
            <br />
            <br />
            <a href='%s'>Download DeskSMS for Android</a>
            </body></html>
            """ % (personal_message, referral_link)

            try:
                message.send()
            except Exception, exception:
                logging.error(email)
                logging.error(exception)
                logging.error(traceback.format_exc(exception))
                logging.error('retrying with noreply')
                message.sender = "%s <noreply@desksms.appspotmail.com>" % (email)
                message.send()

            self.dumps({"success": True})
            
            logging.info("referral email sent")

        except Exception, exception:
            logging.error(email)
            logging.error(exception)
            logging.error(traceback.format_exc(exception))
            self.dumps({"error": "Unknown error!"})

class Referral(db.Model):
    referrer = db.StringProperty()
    referral_date = db.IntegerProperty(indexed = False)

class ReferHandler(webapp2.RequestHandler):
    def get(self):
        try:
            referrer = self.request.get('referrer')
            referral = self.request.get('referral')

            key = db.Key.from_path('Referral', referral)
            existing_referral = db.get(key)
            
            # this email already has a referral
            if existing_referral and existing_referral.referral_date:
                return
            
            # create the new referral
            now = int(time.time() * 1000)
            new_referral = Referral(key_name = referral, referrer = referrer)
            db.put(new_referral)

        except Exception, exception:
            logging.error(exception)
            logging.error(traceback.format_exc(exception))

        finally:
            self.redirect('https://market.android.com/details?id=com.koushikdutta.desktopsms')

class ClearCacheHandler(APIHandler):
    def get(self):
        email = self.check_authorization()
        if email is None:
            return

        key_string = 'Whoami/' + email
        ret = memcache.delete(key_string)

app = webapp2.WSGIApplication([('/login', MainHandler),
                                            ('/_ah/xmpp/subscription/subscribed/', SubscribedHandler),
                                            ('/_ah/xmpp/message/chat/', ChatHandler),
#                                            ('/_ah/xmpp/presence/available/', AvailableHandler),
                                            MailHandler.mapping(),

                                            ('/api/v1/register', RegisterHandler),
                                            ('/api/v1/proxy', ProxyHandler),
                                            ('/api/v1/login', LoginHandler),
                                            ('/api/v1/logout', LogoutHandler),
                                            ('/api/v1/ping', PingHandler),

                                            # TODO remove following 4
                                            ('/api/v1/whoami', WhoamiHandler),
                                            ('/api/v1/user/whoami', WhoamiHandler),
                                            ('/api/v1/user/login', LoginHandler),
                                            ('/api/v1/user/logout', LogoutHandler),

                                            ('/api/v1/user/.*?/dial', DialHandler),
                                            ('/api/v1/user/.*?/settings', SettingsHandler),
                                            ('/api/v1/user/.*?/badge', BadgeHandler),
                                            ('/api/v1/user/.*?/sms', SmsHandler),
                                            ('/api/v1/user/.*?/getsms', SmsHandler),
                                            ('/api/v1/user/.*?/image/.*', MmsImageHandler),
                                            ('/api/v1/user/.*?/delete/conversation', DeleteConversationHandler),
                                            ('/api/v1/user/.*?/call', CallHandler),
                                            ('/api/v1/user/.*?/outbox', OutboxHandler),
                                            ('/api/v1/user/.*?/whoami', WhoamiHandler),
                                            ('/api/v1/user/.*?/status', StatusHandler),
                                            ('/api/v1/user/.*?/read', ReadHandler),
                                            ('/api/v1/user/.*?/ping', PingHandler),
                                            ('/api/v1/user/.*?/pong', PongHandler),
                                            ('/api/v1/user/.*?/tickle', SimpleTickleHandler),
                                            ('/api/v1/user/.*?/push', PushHandler),
                                            ('/api/v1/user/.*?/referral', ReferralHandler),

                                            ('/api/v1/user/.*?/clearcache', ClearCacheHandler),

                                            ('/refer', ReferHandler),
                                            ('/purge', PurgeHandler),
                                            ('/pushping', PushPingHandler),
                                            ('/register', RegisterHandler),
#                                            ('/test', TestHandler),
                                            ('/tickle.*', TickleHandler)],
                                         debug=True)
