from google.appengine.ext import db
import logging
import json as simplejson
import traceback
from google.appengine.api import mail
import urllib
import sys

setattr(db.Property, 'json_serialize', True)

from handlers import APIHandler

class UserObject(db.Model):
    email = db.StringProperty()
    email.json_serialize = False

    known_types = {}

    @staticmethod
    def store_object(instance, data):
        props = UserObject.known_types.get(instance.__class__, None)
        if not props:
            props = {}
            properties = instance.properties().items() 
            for field, value in properties:
                props[field] = value
            UserObject.known_types[instance.__class__] = props
            logging.info(props)

        for name in data:
            val = data[name]
            prop = props.get(name, None)
            if not prop or not prop.json_serialize:
                continue
            if (isinstance(val, str) or isinstance(val, unicode)) and len(val) > 500 and not isinstance(prop, db.TextProperty):
                logging.error(len(val))
                logging.error(prop)
                setattr(instance, name, val[:500])
            else:
                if isinstance(prop, db.TextProperty):
                    logging.info('TextProperty')
                    logging.info(len(val))
                setattr(instance, name, val)

        instance.json_deserialize(data)

    def json_serialize(self, dict):
        dict['key'] = '/'.join(unicode(self.key().id_or_name()).split('/')[1:])

    def json_deserialize(self, data):
        pass


class UserObjectHandler(APIHandler):
    email = None

    def query_objects(self, email, columns):
        object_type = self.get_object_type()

        offset = self.get_request_int_argument('offset')
        limit = self.get_request_int_argument('limit')

        if limit is not None:
            if offset is not None:
                constraint = "LIMIT %d,%d" % (offset, limit)
            else:
                constraint = "LIMIT %d" % (limit)
        elif offset is not None:
            constraint = "OFFSET %d" % (offset)
        else:
            constraint = ''

        key_string = self.request.get('key', None)
        if key_string is None:
            custom_where = self.where()
            where_args = (email,) + custom_where[1]
            logging.info(where_args)
            logging.info(custom_where)
            data = db.GqlQuery("SELECT %s FROM %s WHERE email=:1 %s ORDER BY %s %s" % (columns, object_type.__name__, custom_where[0], self.order_by(), constraint), *where_args)
            data = list(data)
        else:
            logging.info('key path')
            key_string = email + '/' + key_string
            logging.info(key_string)
            key = db.Key.from_path(object_type.__name__, key_string)
            if columns == '*':
                value = db.get(key)
                if value is None:
                    data = []
                else:
                    data = [value]
            else:
                return [key]

        return data

    def delete(self):
        email = self.check_authorization()
        if email is None:
            return
        self.email = email
        keys = self.query_objects(email, '__key__')
        db.delete_async(keys)
        self.dumps({'success': True})

    def should_put(self, email, envelope, put):
        return True

    def should_process(self, email, envelope):
        return True

    def where(self):
        return ("", ())

    def on_put(self, email, envelope, put):
        logging.info(email, put)

    def dumps_raw(self, obj):
        APIHandler.dumps(self, obj)

    def dumps(self, obj):
        envelope = { 'email': self.email, 'data': obj }

        url_parts = self.request.path.split('/')
        bucket = url_parts[len(url_parts) - 1]
        bucket = urllib.unquote(bucket)
        envelope['type'] = bucket

        APIHandler.dumps(self, envelope)

    def get(self):
        operation = self.request.get("operation", None)
        if operation == "POST":
            envelope = simplejson.loads(self.request.get('data'))
            self.post_internal(envelope)
            return

        if operation == "DELETE":
            self.delete()
            return

        email = self.check_authorization()
        if email is None:
            return
        self.email = email
        logging.info(email)

        data = self.query_objects(email, '*')
        self.dumps(data)

        # sms = Sms(key_name = 'koush@koushikdutta.com/+12065528017/1234568')
        # sms.type = 2
        # sms.message = 'hi there'
        # sms.number = '+12065528017'
        # sms.date = 1234568
        # sms.email = 'koush@koushikdutta.com'
        # db.put(sms)

    def post(self):
        logging.info('start')
        try:
            envelope = simplejson.loads(self.request.body)
        except Exception, exception:
            try:
                pass
                #message = mail.EmailMessage(sender="noreply@desksms.appspotmail.com", subject="error")
                #message.to = "koush@clockworkmod.com"
                #message.body = self.request.body
                #message.send()
            except:
                pass
            # the client is getting stuck in a loop sending garbage...
            logging.error(exception)
            logging.error(traceback.format_exc(exception))
            logging.error(self.request.body)
            self.dumps({"success": True, "warning": "Unparseable payload?"})
            return

        self.post_internal(envelope)

    def post_internal(self, envelope):
        email = self.check_authorization()
        if email is None:
            logging.info('email is none')
            return
        self.email = email
        logging.info(email)
        
        if not self.should_process(email, envelope):
            return

        #logging.info(envelope)
        data = envelope['data']

        logging.info('this_last_sync:')
        logging.info(envelope.get('this_last_sync', None))
        logging.info('next_last_sync:')
        logging.info(envelope.get('next_last_sync', None))

        put = []
        for d in data:
            try:
                o = self.new(email, d)
                UserObject.store_object(o, d)
                o.email = email
                put.append(o)
            except Exception, exception:
                # return an error code so the client craps out and syncs again?
                logging.error(exception)
                logging.error(traceback.format_exc(exception))

        if self.should_put(email, envelope, put):
            if len(put):
                logging.info('this put WILL persist data: %s' % len(put))
                if len(put) > 50:
                    logging.error('large put?')
                    logging.error(envelope)
                db.put_async(put)
            else:
                logging.info('this put will not persist data')
        else:
            logging.info('this put will not persist data')

        try:
            results = self.on_put(email, envelope, put)
            if not results:
                self.dumps_raw({'success': True})
            else:
                self.dumps_raw(results)
        except Exception, exception:
            # return an error code so the client craps out and syncs again?
            logging.error(exception)
            logging.error(traceback.format_exc(exception))
            self.dumps_raw({'error': 'unknown error'})

        logging.info('end')
