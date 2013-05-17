import datetime  
import time 

from google.appengine.api import users 
from google.appengine.ext import db 
import json as simplejson

class GqlEncoder(simplejson.JSONEncoder): 

    """Extends JSONEncoder to add support for GQL results and properties. 

    Adds support to simplejson JSONEncoders for GQL results and properties by 
    overriding JSONEncoder's default method. 
    """ 

    # TODO Improve coverage for all of App Engine's Property types. 

    def default(self, obj): 

        """Tests the input object, obj, to encode as JSON.""" 

        if hasattr(obj, '__json__'): 
            return getattr(obj, '__json__')() 

        if isinstance(obj, db.GqlQuery): 
            return list(obj) 

        elif isinstance(obj, db.Model): 
            properties = obj.properties().items() 
            output = {} 
            for field, value in properties:
                if value.json_serialize:
                    output[field] = getattr(obj, field)
            if hasattr(obj, 'json_serialize'):
                obj.json_serialize(output)
            return output 

        elif isinstance(obj, datetime.datetime): 
            output = {} 
            fields = ['day', 'hour', 'microsecond', 'minute', 'month', 'second', 'year'] 
            methods = ['ctime', 'isocalendar', 'isoformat', 'isoweekday', 'timetuple'] 
            for field in fields: 
                output[field] = getattr(obj, field) 
            for method in methods: 
                output[method] = getattr(obj, method)() 
            output['epoch'] = time.mktime(obj.timetuple()) 
            return output 

        elif isinstance(obj, time.struct_time): 
            return list(obj) 

        elif isinstance(obj, users.User): 
            output = {} 
            methods = ['nickname', 'email', 'auth_domain'] 
            for method in methods: 
                output[method] = getattr(obj, method)() 
                return output 

        return simplejson.JSONEncoder.default(self, obj) 
