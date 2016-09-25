import time
import sys
import os
import urllib
import urllib2
import json
from operator import itemgetter
from collections import defaultdict
from functools import partial
from types import GeneratorType
from base64 import b64encode
import signal

import setupfile
from extras import json_encode, json_decode, DotDict
from dispatch import JobError
from status import print_status_stacks
import unixhttp; unixhttp # for unixhttp:// URLs, as used to talk to the daemon


siginfo_received = False
def siginfo(sig, frame):
    global siginfo_received
    siginfo_received = True

def handlesig(sig):
    # Don't do this if we are part of the daemon
    if signal.getsignal(sig) == signal.SIG_DFL:
        signal.signal(sig, siginfo)
        signal.siginterrupt(sig, False)

handlesig(signal.SIGUSR1)
if hasattr(signal, 'SIGINFO'):
    handlesig(signal.SIGINFO)

class Automata:
    """
    Launch jobs, wait for completion.
    """

    def __init__(self, server_url, dataset='churn', verbose=False, flags=None, subjob_cookie=None):
        """
        Set server url and legacy dataset parameter
        """
        self.dataset = dataset
        self.url = server_url
        self.subjob_cookie = subjob_cookie
        self.history = []
        self.verbose = verbose
        self.monitor = None
        self.flags = flags or []
        # Workspaces should be per Automata
        from jobid import put_workspaces
        put_workspaces(self.list_workspaces())
        self.update_method_deps()
        self.clear_record()

    def clear_record(self):
        self.record = defaultdict(JobList)
        self.jobs = self.record[None]

    def validate_response(self, response):
        # replace with homemade function,
        # this is run on bigdata response
        pass


    def remake(self, jobid, part='all'):
        if part=='all':
            url = self.url+'/update/'+jobid
        elif part in ['prepare', 'analysis', 'synthesis', ]:
            url = self.url+'/update/'+jobid+'/'+part
        else:
            print "PROBLEM IN REMAKE!", jobid, part
            exit(1)
        resp = urllib2.urlopen(url).read()
        print resp
        self._wait(time.time())

    def abort(self):
        return json.loads(urllib2.urlopen(self.url+'/abort').read())

    def info(self):
        resp = json.loads(urllib2.urlopen(self.url+'/workspace_info').read())
        return DotDict(resp)

    def config(self):
        resp = json.loads(urllib2.urlopen(self.url+'/config').read())
        return DotDict(resp)

    def set_workspace(self, workspace):
        resp = urllib2.urlopen(self.url+'/set_workspace/'+workspace).read()
        print resp
        

    def new(self, method, caption=None):
        """
        Prepare submission of a new job.
        """
        self.params = defaultdict(lambda: {'options': {}, 'datasets': {}, 'jobids': {}})
        self.job_method = method
        if not caption:
            self.job_caption='fsm_'+method
        else:
            self.job_caption = caption

    def options(self, method, optionsdict):
        """
        Append options for "method".
        This method could be called repeatedly for all
        included methods.
        """
        self.params[method]['options'].update(optionsdict)

    def datasets(self, method, datasetdict):
        """
        Similar to self.options(), but for datasets.
        """
        self.params[method]['datasets'].update(datasetdict)

    def jobids(self, method, jobiddict):
        """
        Similar to self.options(), but for jobids.
        """
        self.params[method]['jobids'].update(jobiddict)

    def submit(self, wait=True, why_build=False):
        """
        Submit job to server and conditionaly wait for completion.
        """
        if not why_build and 'why_build' in self.flags:
            why_build = 'on_build'
        if self.monitor and not why_build:
            self.monitor.submit(self.job_method)
        data = setupfile.generate(self.job_caption, self.job_method, self.params, why_build=why_build)
        if self.subjob_cookie:
            data.subjob_cookie = self.subjob_cookie
            data.parent_pid = os.getpid()
        t0 = time.time()
        self.job_retur = self._server_submit(data)
        self.history.append((data, self.job_retur))
        #
        if wait and not self.job_retur.done:
            self._wait(t0)
        if self.monitor and not why_build:
            self.monitor.done()

    def _wait(self, t0):
        global siginfo_received
        idle, status_stacks, current = self._server_idle(0)
        if idle:
            return
        waited = int(round(time.time() - t0)) - 1
        if self.verbose == 'dots':
            print '[' + '.' * waited,
        while not idle:
            if siginfo_received:
                print_status_stacks(status_stacks)
                siginfo_received = False
            waited += 1
            if waited % 60 == 0 and self.monitor:
                self.monitor.ping()
            if self.verbose:
                now = time.time()
                if current:
                    current = (now - t0, current[1], now - current[2],)
                else:
                    current = (now - t0, self.job_method, 0,)
                if self.verbose == 'dots':
                    if waited % 60 == 0:
                        sys.stdout.write('[%d]\n ' % (now - t0,))
                    else:
                        sys.stdout.write('.')
                elif self.verbose == 'log':
                    if waited % 60 == 0:
                        print '%d seconds, still waiting for %s (%d seconds)' % current
                else:
                    sys.stdout.write('\r\033[K           %.1f %s %.1f' % current)
            idle, status_stacks, current = self._server_idle(1)
        if self.verbose == 'dots':
            print '(%d)]' % (time.time() - t0,)
        else:
            print '\r\033[K           %d seconds' % (round(time.time() - t0),)

    def jobid(self, method):
        """
        Return jobid of "method"
        """
        return self.job_retur.jobs[method].link

    def dump_history(self):
        return self.history

    def _server_idle(self, timeout=0):
        """ask server if it is idle, return (idle, status_stacks)"""
        if self.verbose:
            url = self.url + '/status/full'
        else:
            url = self.url + '/status'
        url += '?subjob_cookie=%s&timeout=%d' % (self.subjob_cookie or '', timeout,)
        resp = json_decode(urllib2.urlopen(url).read())
        last_error = resp.last_error
        if last_error:
            print >>sys.stderr, "\nFailed to build jobs:"
            for jobid, method, status in last_error:
                e = JobError(jobid, method, status)
                print >>sys.stderr, e.format_msg()
            raise e
        return resp.idle, resp.status_stacks, resp.current

    def _server_submit(self, json):
        # submit json to server
        postdata = urllib.urlencode({'json': setupfile.encode_setup(json)})
        resp = urllib2.urlopen(self.url+'/submit', data=postdata)
        res = resp.read()
        resp.close()
        res = json_decode(res)
        if res.error:
            raise Exception("Submit failed: " + res.error)
        if not res.why_build:
            if not self.subjob_cookie:
                self._printlist(res.jobs)
            self.validate_response(res.jobs)
        return res

    def _printlist(self, returndict):
        # print (return list) in neat format
        for method, item in sorted(returndict.items(), key=lambda x: x[1].link):
            if item.make == True:
                make_msg = 'MAKE'
            else:
                make_msg = item.make or 'link'
            print '        -  %44s' % method.ljust(44),
            print ' %s' % (make_msg,),
            print ' %s' % item.link,
            print

    def method_info(self, method):
        resp = json.loads(urllib2.urlopen(self.url+'/method_info/'+method).read())
        return resp

    def methods_info(self):
        resp = json.loads(urllib2.urlopen(self.url+'/methods/').read())
        return resp

    def update_methods(self):
        resp = urllib2.urlopen(self.url+'/update_methods').read()
        self.update_method_deps()
        return resp

    def update_method_deps(self):
        info = self.methods_info()
        self.dep_methods = {str(name): set(map(str, data.get('dep', ()))) for name, data in info.iteritems()}

    def list_workspaces(self):
        return json.loads(urllib2.urlopen(self.url+'/list_workspaces/').read())

    def call_method(self, method, defopt={}, defdata={}, defjob={}, options=(), datasets=(), jobids=(), record_in=None, record_as=None, why_build=False, caption=None):
        todo  = {method}
        org_method = method
        opted = set()
        self.new(method, caption)
        # options and datasets can be for just method, or {method: options, ...}.
        def dictofdicts(d):
            if method not in d:
                return {method: dict(d)}
            else:
                return dict(d)
        options  = dictofdicts(options)
        datasets = dictofdicts(datasets)
        jobids   = dictofdicts(jobids)
        def resolve_something(res_in, d):
            def resolve(name, inner=False):
                if name is None and not inner:
                    return None
                if isinstance(name, JobTuple):
                    names = [str(name)]
                elif isinstance(name, (list, tuple)):
                    names = name
                else:
                    assert isinstance(name, (str, unicode)), "%s: %s" % (key, name)
                    names = [name]
                fixed_names = []
                for name in names:
                    res_name = res_in.get(name, name)
                    if isinstance(res_name, (list, tuple)):
                        res_name = resolve(res_name, True)
                    assert isinstance(res_name, (str, unicode)), "%s: %s" % (key, name) # if name was a job-name this gets a dict and dies
                    fixed_names.append(res_name)
                return ','.join(fixed_names)
            for key, name in d.iteritems():
                yield key, resolve(name)
        resolve_datasets = partial(resolve_something, defdata)
        resolve_jobids   = partial(resolve_something, defjob)
        to_record = []
        while todo:
            method = todo.pop()
            m_opts = dict(defopt.get(method, ()))
            m_opts.update(options.get(method, ()))
            self.options(method, m_opts)
            m_datas = dict(defdata.get(method, ()))
            m_datas.update(resolve_datasets(datasets.get(method, {})))
            self.datasets(method, m_datas)
            m_jobs = dict(defjob.get(method, ()))
            m_jobs.update(resolve_jobids(jobids.get(method, {})))
            self.jobids(method, m_jobs)
            opted.add(method)
            to_record.append(method)
            todo.update(self.dep_methods[method])
            todo.difference_update(opted)
        self.submit(why_build=why_build)
        if why_build: # specified by caller
            return self.job_retur.why_build
        if self.job_retur.why_build: # done by server anyway (because --flags why_build)
            print "Would have built from:"
            print "======================"
            print setupfile.encode_setup(self.history[-1][0])
            print "Could have avoided build if:"
            print "============================"
            print json_encode(self.job_retur.why_build)
            print
            from inspect import stack
            stk = stack()[1]
            print "Called from %s line %d" % (stk[1], stk[2],)
            exit()
        if isinstance(record_as, str):
            record_as = {org_method: record_as}
        elif not record_as:
            record_as = {}
        for m in to_record:
            self.record[record_in].insert(record_as.get(m, m), self.jobid(m))
        return self.jobid(org_method)


class JobTuple(tuple):
    """
    A tuple of (method, jobid) with accessor properties that gives just
    the jobid for str and etree (encode).
    """
    def __new__(cls, *a):
        if len(a) == 1: # like tuple
            method, jobid = a[0]
        else: # like namedtuple
            method, jobid = a
        assert isinstance(method, (str, unicode))
        assert isinstance(jobid, (str, unicode))
        return tuple.__new__(cls, (str(method), str(jobid)))
    method = property(itemgetter(0), doc='Field 0')
    jobid  = property(itemgetter(1), doc='Field 1')
    def __str__(self):
        return self.jobid
    def encode(self, encoding=None, errors="strict"):
        """Unicode-object compat. For etree, gives jobid."""
        return str(self).encode(encoding, errors)

class JobList(list):
    """
    Mostly a list, but uses the jobid of the last element in str and etree (encode).
    Also provides the following properties:
    .all for an a,b,c string (jobids)
    .method for the latest method.
    .jobid for the latest jobid.
    .pretty for a pretty-printed version.
    Taking a single element gives you a (method, jobid) tuple
    (which also gives jobid in str and etree).
    Taking a slice gives a jobid,jobid,... string.
    There is also .find, for finding the latest jobid with a given method.
    """

    def __init__(self, *a):
        if len(a) == 1:
            self.extend(a[0])
        elif a:
            self.insert(*a)

    def insert(self, method, jobid):
        list.append(self, JobTuple(method, jobid))

    def append(self, *a):
        if len(a) == 1:
            data = a[0]
        else:
            return self.insert(*a)
        if isinstance(data, (str, unicode)):
            return self.insert('', data)
        if isinstance(data, (tuple, list)):
            return self.insert(*data)
        raise ValueError("What did you try to append?", data)

    def extend(self, other):
        if isinstance(other, (str, unicode, JobTuple)):
            return self.append(other)
        if not isinstance(other, (list, tuple, GeneratorType)):
            raise ValueError("Adding what?", other)
        for item in other:
            self.append(item)

    def __str__(self):
        """Last element jobid, for convenience."""
        if self:
            return self[-1].jobid
        else:
            return ''
    def __unicode__(self):
        """Last element jobid, for convenience."""
        return unicode(str(self))
    def __repr__(self):
        return "%s(%s)" % (type(self).__name__, list.__repr__(self))

    # this is what etree calls
    def encode(self, encoding=None, errors="strict"):
        """Unicode-object compat. For etree, gives last element."""
        return str(self).encode(encoding, errors)
    def __getslice__(self, i, j): # This is friggin pre-python 2, and I *still* need it.
        return self[slice(i, j)]
    def __getitem__(self, item):
        if isinstance(item, slice):
            return JobList(list.__getitem__(self, item))
        elif isinstance(item, (str, unicode)):
            return self.find(item)[-1] # last matching or IndexError
        else:
            return list.__getitem__(self, item)
    def __delitem__(self, item):
        if isinstance(item, (int, slice)):
            return list.__delitem__(self, item)
        if isinstance(item, tuple):
            self[:] = [j for j in self if item != j]
        else:
            self[:] = [j for j in self if item not in j]
    def __add__(self, other):
        if not isinstance(other, list):
            raise ValueError("Adding what?", other)
        return JobList(list(self) + other)
    def __iadd__(self, other):
        self.extend(other)
        return self
    @property
    def all(self):
        """Comma separated list of all elements' jobid"""
        return ','.join(e.jobid for e in self)

    @property
    def method(self):
        if self:
            return self[-1].method
    @property
    def jobid(self): # for symmetry
        if self:
            return self[-1].jobid

    @property
    def pretty(self):
        """Formated for printing"""
        if not self: return 'JobList([])'
        template = '   [%%3d] %%%ds : %%s' % (max(len(i.method) for i in self),)
        return 'JobList(\n' + \
            '\n'.join(template % (i, a, b) for i, (a, b) in enumerate(self)) + \
            '\n)'

    def find(self, method):
        """Matching elements returned as new Joblist."""
        return JobList(e for e in self if e.method == method)

    def get(self, method, default=None):
        l = self.find(method)
        return l[-1] if l else default

def profile_jobs(jobs):
    from extras import job_post
    if isinstance(jobs, str):
        jobs = [jobs]
    total = 0
    seen = set()
    for j in jobs:
        if isinstance(j, tuple):
            j = j[1]
        if j not in seen:
            total += job_post(j).profile.total
            seen.add(j)
    return total


class UrdResponse(dict):
	def __new__(cls, d):
		assert cls is UrdResponse, "Always make these through UrdResponse"
		obj = dict.__new__(UrdResponse if d else EmptyUrdResponse)
		return obj

	def __init__(self, d):
		d = dict(d or ())
		d.setdefault('caption', '')
		d.setdefault('timestamp', '0')
		d.setdefault('joblist', JobList())
		d.setdefault('deps', {})
		dict.__init__(self, d)

	__setitem__ = dict.__setitem__
	__delattr__ = dict.__delitem__
	def __getattr__(self, name):
		if name.startswith('_') or name not in self:
			raise AttributeError(name)
		return self[name]

	@property
	def as_dep(self):
		return DotDict(timestamp=self.timestamp, joblist=self.joblist, caption=self.caption, _default=lambda: None)

class EmptyUrdResponse(UrdResponse):
	def __nonzero__(self):
		return False # so you can do "if urd.latest('foo'):" and similar.

def _urd_typeify(d):
	if isinstance(d, str):
		d = json.loads(d)
		if not d or isinstance(d, unicode):
			return d
	res = DotDict(_default=lambda: None)
	for k, v in d.iteritems():
		if k == 'joblist':
			v = JobList(v)
		elif isinstance(v, dict):
			v = _urd_typeify(v)
		res[k] = v
	return res

class Urd(object):
	def __init__(self, a, info, user, password, horizon=None):
		self._a = a
		self._url = info.urd
		assert '://' in str(info.urd), 'Bad urd URL: %s' % (info.urd,)
		self._user = user
		self._current = None
		self.info = info
		self.flags = set(a.flags)
		self.horizon = horizon
		self.joblist = a.jobs
		auth = b64encode('%s:%s' % (user, password,))
		self._headers = {'Content-Type': 'application/json', 'Authorization': 'Basic ' + auth}

	def _path(self, path):
		if '/' not in path:
			path = '%s/%s' % (self._user, path,)
		return path

	def _call(self, url, data=None, fmt=_urd_typeify):
		url = url.replace(' ', '%20')
		if data is not None:
			req = urllib2.Request(url, json.dumps(data), self._headers)
		else:
			req = urllib2.Request(url)
		tries_left = 3
		while True:
			try:
				r = urllib2.urlopen(req)
				try:
					return fmt(r.read())
				finally:
					try:
						r.close()
					except Exception:
						pass
			except urllib2.HTTPError as e:
				if e.code in (401, 409,):
					raise
				tries_left -= 1
				if not tries_left:
					raise
				print >>sys.stderr, 'Error %d from urd, %d tries left' % (e.code, tries_left,)
			except ValueError:
				tries_left -= 1
				if not tries_left:
					raise
				print >>sys.stderr, 'Bad data from urd, %d tries left' % (tries_left,)
			except urllib2.URLError:
				print >>sys.stderr, 'Error contacting urd'
			time.sleep(4)

	def _get(self, path, *a):
		assert self._current, "Can't record dependency with nothing running"
		path = self._path(path)
		assert path not in self._deps, 'Duplicate ' + path
		url = '/'.join((self._url, path,) + a)
		res = UrdResponse(self._call(url))
		if res:
			self._deps[path] = res.as_dep
		self._latest_joblist = res.joblist
		return res

	def _latest_str(self):
		if self.horizon:
			return '<=' + self.horizon
		else:
			return 'latest'

	def get(self, path, timestamp):
		return self._get(path, timestamp)

	def latest(self, path):
		return self.get(path, self._latest_str())

	def first(self, path):
		return self.get(path, 'first')

	def peek(self, path, timestamp):
		path = self._path(path)
		url = '/'.join((self._url, path, timestamp,))
		return UrdResponse(self._call(url))

	def peek_latest(self, path):
		return self.peek(path, self._latest_str())

	def peek_first(self, path):
		return self.peek(path, 'first')

	def since(self, path, timestamp):
		path = self._path(path)
		url = '%s/%s/since/%s' % (self._url, path, timestamp,)
		return self._call(url, fmt=json.loads)

	def begin(self, path, timestamp=None, caption=None, update=False):
		assert not self._current, 'Tried to begin %s while running %s' % (path, self._current,)
		self._current = self._path(path)
		self._current_timestamp = timestamp
		self._current_caption = caption
		self._update = update
		self._deps = {}
		self._a.clear_record()
		self.joblist = self._a.jobs
		self._latest_joblist = None

	def abort(self):
		self._current = None

	def finish(self, path, timestamp=None, caption=None):
		path = self._path(path)
		assert self._current, 'Tried to finish %s with nothing running' % (path,)
		assert path == self._current, 'Tried to finish %s while running %s' % (path, self._current,)
		user, automata = path.split('/')
		self._current = None
		caption = caption or self._current_caption or ''
		timestamp = timestamp or self._current_timestamp
		assert timestamp, 'No timestamp specified in begin or finish for %s' % (path,)
		data = DotDict(
			user=user,
			automata=automata,
			joblist=self.joblist,
			deps=self._deps,
			caption=caption,
			timestamp=timestamp,
		)
		if self._update:
			data.flags = ['update']
		url = self._url + '/add'
		return self._call(url, data)

	def truncate(self, path, timestamp):
		url = '%s/truncate/%s/%s' % (self._url, self._path(path), timestamp,)
		return self._call(url, '')

	def build(self, method, options={}, datasets={}, jobids={}, name=None, caption=None, why_build=False):
		return self._a.call_method(method, options={method: options}, datasets={method: datasets}, jobids={method: jobids}, record_as=name, caption=caption, why_build=why_build)

	def build_chained(self, method, options={}, datasets={}, jobids={}, name=None, caption=None, why_build=False):
		datasets = dict(datasets or {})
		assert 'previous' not in datasets, "Don't specify previous dataset to build_chained"
		assert name, "build_chained must have 'name'"
		assert self._latest_joblist is not None, "Can't build_chained without a dependency to chain from"
		datasets['previous'] = self._latest_joblist.get(name)
		return self.build(method, options, datasets, jobids, name, caption, why_build)