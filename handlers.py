from google.appengine.ext import webapp
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
import gqlencoder
from google.appengine.api import oauth
import traceback

from google.appengine.ext.db import Property
from google.appengine.api import memcache

import random
import base64
import hashlib

from urlparse import urlparse

import webapp2


class APIHandler(webapp2.RequestHandler):
    user = None

    def get_request_int_argument(self, name):
        val = self.request.get(name, None)
        if val is not None:
            try:
                val = int(val)
            except:
                val = None
                pass
        return val
        
    def dumps(self, obj):
        callback = self.request.get('callback', None)
        if callback is not None:
            self.response.headers['Content-Type'] = 'application/javascript'
            self.response.out.write("%s(%s)" % (callback, simplejson.dumps(obj, cls=gqlencoder.GqlEncoder)))
        else:
            self.response.headers['Content-Type'] = 'application/json'
            self.response.out.write(simplejson.dumps(obj, cls=gqlencoder.GqlEncoder))

    def check_authorization(self, user_check = True):
        referer = self.request.headers.get('referer', None)
        callback = self.request.get('callback', None)

        if callback is not None and referer is not None:
            parsed_referer = urlparse(referer)
            allowed = ['localhost', 'localhost:3000', 'localhost:8080', 'localhost:3001', 'www.clockworkmod.com', 'desksms.clockworkmod.com', 'desksms.deployfu.com', 'desksms.appspot.com', '2.desksms.appspot.com']

            if parsed_referer.netloc not in allowed:
                self.dumps({'error': 'jsonp requests from this domain is not supported'})
                return

            self.response.headers['Access-Control-Allow-Origin'] = parsed_referer.netloc

        current_user = users.get_current_user()
        is_admin = users.is_current_user_admin()
        if current_user is None:
            try:
                current_user = oauth.get_current_user()
                is_admin = oauth.is_current_user_admin()
            except:
                pass

        if current_user is None:
            if user_check:
                logging.info('user is not logged in')
                self.redirect(users.create_login_url("/"))
            #self.dumps({'error': 'not logged in'})
            return

        current_user_email = current_user.email().lower()
        # if this is a user data path, verify that the current
        # user has proper access.
        if user_check:
            email = urllib.unquote(self.request.path.split('/')[4]).lower()
            if email == 'default':
                email = current_user_email
            elif email != current_user_email and not is_admin:
                logging.info(email)
                logging.info(current_user_email)
                logging.info('not admin')
                self.dumps({'error': 'not administrator'})
                return
        else:
            email = current_user_email

        logging.info(email)
        self.user = current_user
        return email
