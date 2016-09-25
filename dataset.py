# -*- coding: utf-8 -*-

from __future__ import division
from __future__ import unicode_literals

import os
from keyword import kwlist
from collections import namedtuple
from itertools import compress
from functools import partial

import blob
from extras import DotDict
from jobid import resolve_jobid_filename
from gzwrite import typed_writer

kwlist = set(kwlist)
# Add some python3 keywords
kwlist.update({'False', 'None', 'True', 'nonlocal', 'async', 'await'})
iskeyword = frozenset(kwlist).__contains__

# A dataset is defined by a pickled DotDict containing at least the following (all strings are unicode):
#     version = (2, 1,),
#     filename = "filename" or None,
#     hashlabel = "column name" or None,
#     caption = "caption",
#     columns = {"column name": DatasetColumn,},
#     previous = "previous_jid/datasetname" or None,
#     parent = "parent_jid/datasetname" or None,
#     lines = [line, count, per, slice,],
#     cache = ((id, data), ...), # key is missing if there is no cache in this dataset
#     cache_distance = datasets_since_last_cache, # key is missing before version 2, 1 or if previous is None
# A version 1, 0 dataset has an older DatasetColumn with less specific filenames for the data and no offsets.
#
# A DatasetColumn has these fields:
#     type = "type", # something that exists in type2iter
#     name = "name", # a clean version of the column name, valid in the filesystem and as a python identifier.
#     location = something, # where the data for this column lives
#         in version 1 this is "jobid/datasetname"
#         in version 2 this is "jobid/path/to/file" if .offsets else "jobid/path/with/%s/for/sliceno"
#     min = minimum value in this dataset or None
#     max = maximum value in this dataset or None
#     offsets = (offset, per, slice) or None for non-merged slices.
#
# Going from a DatasetColumn to a filename is like this for version 1 datasets:
#     jid, name = dc.location.split('/')
#     resolve_jobid_filename(jid, '%s/%d/%s' % (name, sliceno, dc.name,))
# and like this for version 2 datasets:
#     jid, path = dc.location.split('/', 1)
#     if dc.offsets:
#         resolve_jobid_filename(jid, path)
#         seek to dc.offsets[sliceno], read only ds.lines[sliceno] values.
#     else:
#         resolve_jobid_filename(jid, path % sliceno)
# There is a ds.column_filename function to do this for you (not the seeking, obviously).
#
# The dataset pickle is jid/name/dataset.pickle, so jid/default/dataset.pickle for the default dataset.

def _uni(s):
	if s is None:
		return None
	if isinstance(s, bytes):
		try:
			return s.decode('utf-8')
		except UnicodeDecodeError:
			return s.decode('iso-8859-1')
	return unicode(s)

def _clean_name(n, seen_n):
	n = ''.join(c if c.isalnum() else '_' for c in n)
	if n[0].isdigit():
		n = '_' + n
	while n in seen_n or iskeyword(n):
		n += '_'
	seen_n.add(n)
	return n

def _dsid(t):
	if not t:
		return None
	if isinstance(t, (tuple, list)):
		jid, name = t
		if not jid:
			return None
		t = '%s/%s' % (jid.split('/')[0], _uni(name) or 'default')
	if '/' not in t:
		t += '/default'
	return _uni(t)

# If we want to add fields to later versions, using a versioned name will
# allow still loading the old versions without messing with the constructor.
_DatasetColumn_1_0 = namedtuple('_DatasetColumn_1_0', 'type name location min max')
_DatasetColumn_2_0 = namedtuple('_DatasetColumn_2_0', 'type name location min max offsets')
DatasetColumn = _DatasetColumn_2_0

class _New_dataset_marker(unicode): pass
_new_dataset_marker = _New_dataset_marker('new')

_ds_cache = {}
def _ds_load(obj):
	n = unicode(obj)
	if n not in _ds_cache:
		_ds_cache[n] = blob.load(obj._name('pickle'), obj.jobid)
		_ds_cache.update(_ds_cache[n].get('cache', ()))
	return _ds_cache[n]

class Dataset(unicode):
	"""
	Represents a dataset. Is also a string 'jobid/name', or just 'jobid' if
	name is 'default' (for better backwards compatibility).
	
	You usually don't have to make these yourself, because datasets.foo is
	already a Dataset instance (or None).
	
	These decay to a (unicode) string when pickled.
	"""

	def __new__(cls, jobid, name=None):
		if isinstance(jobid, (tuple, list)):
			jobid = _dsid(jobid)
		if '/' in jobid:
			assert not name, "Don't pass both a separate name and jobid as jid/name"
			jobid, name = jobid.split('/', 1)
		assert jobid, "If you really meant to use yourself as a dataset, pass params.jobid explicitly."
		name = _uni(name or 'default')
		assert '/' not in name
		if name == 'default':
			suffix = ''
		else:
			suffix = '/' + name
		if jobid is _new_dataset_marker:
			from g import JOBID
			fullname = JOBID + suffix
		else:
			fullname = jobid + suffix
		obj = unicode.__new__(cls, fullname)
		obj.name = _uni(name or 'default')
		if jobid is _new_dataset_marker:
			obj._data = DotDict({
				'version': (2, 1,),
				'filename': None,
				'hashlabel': None,
				'caption': '',
				'columns': {},
				'parent': None,
				'previous': None,
				'lines': [],
			})
			obj.jobid = None
		else:
			obj.jobid = jobid
			obj._data = DotDict(_ds_load(obj))
			if obj._data.version[0] == 1:
				obj._data.columns = {k: DatasetColumn(type=c.type, name=c.name, location="%s/%%s/%s" % (c.location, c.name), min=c.min, max=c.max, offsets=None) for k, c in obj._data.columns.items()}
				obj._data.version = (2, 0)
			assert obj._data.version[0] == 2, "%s/%s: Unsupported dataset pickle version %r" % (jobid, name, obj._data.version,)
			obj._data.columns = dict(obj._data.columns)
		return obj

	# Look like a string after pickling
	def __reduce__(self):
		return unicode, (unicode(self),)

	@property
	def columns(self):
		"""{name: DatasetColumn}"""
		return self._data.columns

	@property
	def previous(self):
		return self._data.previous

	@property
	def parent(self):
		return self._data.parent

	@property
	def filename(self):
		return self._data.filename

	@property
	def hashlabel(self):
		return self._data.hashlabel

	@property
	def caption(self):
		return self._data.caption

	@property
	def lines(self):
		return self._data.lines

	@property
	def shape(self):
		return (len(self.columns), sum(self.lines),)

	def link_to_here(self, name='default'):
		"""Use this to expose a subjob as a dataset in your job:
		Dataset(subjid).link_to_here()
		will allow access to the subjob dataset under your jid."""
		from g import JOBID
		self._data.parent = '%s/%s' % (self.jobid, self.name,)
		self.jobid = _uni(JOBID)
		self.name = _uni(name)
		self._save()

	def _column_iterator(self, sliceno, col, **kw):
		from sourcedata import type2iter
		dc = self.columns[col]
		mkiter = partial(type2iter[dc.type], **kw)
		def one_slice(sliceno):
			fn = self.column_filename(col, sliceno)
			if dc.offsets:
				return mkiter(fn, seek=dc.offsets[sliceno], max_count=self.lines[sliceno])
			else:
				return mkiter(fn)
		if sliceno is None:
			from g import SLICES
			from itertools import chain
			return chain(*[one_slice(s) for s in range(SLICES)])
		else:
			return one_slice(sliceno)

	def _iterator(self, sliceno, columns=None):
		res = []
		not_found = []
		for col in columns or sorted(self.columns):
			if col in self.columns:
				res.append(self._column_iterator(sliceno, col))
			else:
				not_found.append(col)
		assert not not_found, 'Columns %r not found in %s/%s' % (not_found, self.jobid, self.name)
		return res

	def _hashfilter(self, sliceno, hashlabel, it):
		from g import SLICES
		return compress(it, self._column_iterator(None, hashlabel, hashfilter=(sliceno, SLICES)))

	def column_filename(self, colname, sliceno=None):
		dc = self.columns[colname]
		jid, name = dc.location.split('/', 1)
		if dc.offsets:
			return resolve_jobid_filename(jid, name)
		else:
			if sliceno is None:
				sliceno = '%s'
			return resolve_jobid_filename(jid, name % (sliceno,))

	def chain(self, length=-1, reverse=False, stop_jobid=None):
		if stop_jobid:
			# resolve whatever format to the bare jobid
			stop_jobid = Dataset(stop_jobid).jobid
		chain = []
		current = self
		while length != len(chain) and current.jobid != stop_jobid:
			chain.append(current)
			if not current.previous:
				break
			current = Dataset(current.previous)
		if not reverse:
			chain.reverse()
		return chain

	def iterate_chain(self, sliceno, columns=None, length=-1, reverse=False, hashlabel=None, stop_jobid=None, pre_callback=None, post_callback=None, filters=None, translators=None):
		chain = self.chain(length, reverse, stop_jobid)
		return self.iterate_list(sliceno, columns, chain, hashlabel=hashlabel, pre_callback=pre_callback, post_callback=post_callback, filters=filters, translators=translators)

	def iterate(self, sliceno, columns=None, hashlabel=None, filters=None, translators=None):
		return self.iterate_list(sliceno, columns, [self], hashlabel=hashlabel, filters=filters, translators=translators)

	@staticmethod
	def iterate_list(sliceno, columns, jobids, hashlabel=None, pre_callback=None, post_callback=None, filters=None, translators=None):
		from chaining import iterate_datasets
		return iterate_datasets(sliceno, columns, jobids, hashlabel=hashlabel, pre_callback=pre_callback, post_callback=post_callback, filters=filters, translators=translators)

	@staticmethod
	def new(columns, filenames, lines, minmax={}, filename=None, hashlabel=None, caption=None, previous=None, name='default'):
		"""columns = {"colname": "type"}, lines = [n, ...] or {sliceno: n}"""
		columns = {_uni(k): _uni(v) for k, v in columns.items()}
		if hashlabel:
			hashlabel = _uni(hashlabel)
			assert hashlabel in columns, hashlabel
		res = Dataset(_new_dataset_marker, name)
		res._data.lines = list(Dataset._linefixup(lines))
		res._data.hashlabel = hashlabel
		res._append(columns, filenames, minmax, filename, caption, previous, name)
		return res

	@staticmethod
	def _linefixup(lines):
		from g import SLICES
		if isinstance(lines, dict):
			assert set(lines) == set(range(SLICES)), "Lines must be specified for all slices"
			lines = [c for _, c in sorted(lines.items())]
		assert len(lines) == SLICES, "Lines must be specified for all slices"
		return lines

	def append(self, columns, filenames, lines, minmax={}, filename=None, hashlabel=None, hashlabel_override=False, caption=None, previous=None, name='default'):
		if hashlabel:
			hashlabel = _uni(hashlabel)
			if not hashlabel_override:
				assert self.hashlabel == hashlabel, 'Hashlabel mismatch %s != %s' % (self.hashlabel, hashlabel,)
		assert self._linefixup(lines) == self.lines, "New columns don't have the same number of lines as parent columns"
		columns = {_uni(k): _uni(v) for k, v in columns.items()}
		self._append(columns, filenames, minmax, filename, caption, previous, name)

	def _minmax_merge(self, minmax):
		def minmax_fixup(a, b):
			res_min = a[0]
			if res_min is None: res_min = b[0]
			res_max = a[1]
			if res_max is None: res_max = b[1]
			return [res_min, res_max]
		res = {}
		for part in minmax.values():
			for name, mm in part.items():
				omm = minmax_fixup(res.get(name, (None, None,)), mm)
				mm = minmax_fixup(mm, omm)
				res[name] = [min(mm[0], omm[0]), max(mm[1], omm[1])]
		return res

	def _append(self, columns, filenames, minmax, filename, caption, previous, name):
		from sourcedata import type2iter
		from g import JOBID
		jobid = _uni(JOBID)
		name = _uni(name)
		filenames = {_uni(k): _uni(v) for k, v in filenames.items()}
		assert set(columns) == set(filenames), "columns and filenames don't have the same keys"
		if self.jobid and (self.jobid != jobid or self.name != name):
			self._data.parent = '%s/%s' % (self.jobid, self.name,)
		self.jobid = jobid
		self.name = name
		self._data.filename = _uni(filename) or self._data.filename or None
		self._data.caption  = _uni(caption) or self._data.caption or jobid
		self._data.previous = _dsid(previous)
		for n in ('caches', 'cache_distance'):
			if n in self._data: del self._data[n]
		minmax = self._minmax_merge(minmax)
		for n, t in sorted(columns.items()):
			if t not in type2iter:
				raise Exception('Unknown type %s on column %s' % (t, n,))
			mm = minmax.get(n, (None, None,))
			self._data.columns[n] = DatasetColumn(
				type=_uni(t),
				name=filenames[n],
				location='%s/%s/%%s.%s' % (jobid, self.name, filenames[n]),
				min=mm[0],
				max=mm[1],
				offsets=None,
			)
			self._maybe_merge(n)
		self._update_caches()
		self._save()

	def _update_caches(self):
		if self.previous:
			d = Dataset(self.previous)
			cache_distance = d._data.get('cache_distance', 1) + 1
			if cache_distance == 64:
				cache_distance = 0
				chain = self.chain(64)
				self._data.cache = tuple((unicode(d), d._data) for d in chain[1:])
			self._data.cache_distance = cache_distance

	def _maybe_merge(self, n):
		from g import SLICES
		if SLICES < 2:
			return
		fn = self.column_filename(n)
		sizes = [os.path.getsize(fn % (sliceno,)) for sliceno in range(SLICES)]
		if sum(sizes) / SLICES > 524288: # arbitrary guess of good size
			return
		offsets = []
		pos = 0
		with open(fn % ('m',), 'wb') as m_fh:
			for sliceno, size in enumerate(sizes):
				with open(fn % (sliceno,), 'rb') as p_fh:
					data = p_fh.read()
				assert len(data) == size, "Slice %d is %d bytes, not %d?" % (sliceno, len(data), size,)
				os.unlink(fn % (sliceno,))
				m_fh.write(data)
				offsets.append(pos)
				pos += size
		c = self._data.columns[n]
		self._data.columns[n] = c._replace(
			offsets=offsets,
			location=c.location % ('m',),
		)

	def _save(self):
		if not os.path.exists(self.name):
			os.mkdir(self.name)
		blob.save(self._data, self._name('pickle'), temp=False)
		with open(self._name('txt'), 'wb') as fh:
			nl = False
			if self.hashlabel:
				fh.write('hashlabel %s\n' % (self.hashlabel,))
				nl = True
			if self.previous:
				fh.write('previous %s\n' % (self.previous,))
				nl = True
			if nl:
				fh.write('\n')
			col_list = sorted((k, c.type, c.location,) for k, c in self.columns.items())
			lens = tuple(max(minlen, max(len(t[i]) for t in col_list)) for i, minlen in ((0, 4), (1, 4), (2, 8)))
			template = '%%%ds  %%%ds  %%-%ds\n' % lens
			fh.write(template % ('name', 'type', 'location'))
			fh.write(template % tuple('=' * l for l in lens))
			for t in col_list:
				fh.write(template % t)

	def _name(self, thing):
		return '%s/dataset.%s' % (self.name, thing,)

_datasetwriters = {}

_nodefault = object()

class DatasetWriter(object):
	"""
	Create in prepare, use in analysis. Or do the whole thing in
	synthesis.
	
	You can pass these through prepare_res, or get them by trying to
	create a new writer in analysis (don't specify any arguments except
	an optional name).
	
	There are three writing functions with different arguments:
	
	dw.write_dict({column: value})
	dw.write_list([value, value, ...])
	dw.write(value, value, ...)
	
	Values are in the same order as you add()ed the columns (which is in
	sorted order if you passed a dict). The dw.write() function names the
	arguments from the columns too.
	
	If you set hashlabel you can use dw.hashcheck(v) to check if v
	belongs in this slice. You can also just call the writer, and it will
	discard anything that does not belong in this slice.
	
	If you are not in analysis and you wish to use the functions above
	you need to call dw.set_slice(sliceno) first.
	
	If you do not, you can instead get one of the splitting writer
	functions, that select which slice to use based on hashlabel, or
	round robin if there is no hashlabel.
	
	dw.get_split_write_dict()({column: value})
	dw.get_split_write_list()([value, value, ...])
	dw.get_split_write()(value, value, ...)
	
	These should of course be assigned to a local name for performance.
	
	It is permitted (but probably useless) to mix different write or
	split functions, but you can only use either write functions or
	split functions.
	
	You can also use dw.writers[colname] to get a typed_writer and use
	it as you please. The one belonging to the hashlabel will be
	filtering, and returns True if this is the right slice.
	
	If you need to handle everything yourself, set meta_only=True and
	use dw.column_filename(colname) to find the right files to write to.
	In this case you also need to call dw.set_lines(sliceno, count)
	before finishing. You should also call
	dw.set_minmax(sliceno, {colname: (min, max)}) if you can.
	"""

	_split = _split_dict = _split_list = _allwriters_ = None

	def __new__(cls, columns={}, filename=None, hashlabel=None, hashlabel_override=False, caption=None, previous=None, name='default', parent=None, meta_only=False):
		"""columns can be {'name': 'type'} or {'name': DatasetColumn}
		to simplify basing your dataset on another."""
		name = _uni(name)
		assert '/' not in name, name
		from g import running
		if running == 'analysis':
			assert name in _datasetwriters, 'Dataset with name "%s" not created' % (name,)
			assert not columns and not filename and not hashlabel and not caption and not parent, "Don't specify any arguments (except optionally name) in analysis"
			return _datasetwriters[name]
		else:
			assert name not in _datasetwriters, 'Duplicate dataset name "%s"' % (name,)
			os.mkdir(name)
			obj = object.__new__(cls)
			obj._running = running
			obj.filename = _uni(filename)
			obj.hashlabel = _uni(hashlabel)
			obj.hashlabel_override = hashlabel_override,
			obj.caption = _uni(caption)
			obj.previous = _dsid(previous)
			obj.name = _uni(name)
			obj.parent = _dsid(parent)
			obj.columns = {}
			obj.meta_only = meta_only
			obj._clean_names = {}
			if parent:
				obj._pcolumns = Dataset(parent).columns
				obj._seen_n = set(c.name for c in obj._pcolumns.values())
			else:
				obj._pcolumns = {}
				obj._seen_n = set()
			obj._started = False
			obj._lens = {}
			obj._minmax = {}
			obj._order = []
			for k, v in sorted(columns.items()):
				if isinstance(v, tuple):
					v = v.type
				obj.add(k, v)
			_datasetwriters[name] = obj
			return obj

	def add(self, colname, coltype, default=_nodefault):
		from g import running
		assert running == self._running, "Add all columns in the same step as creation"
		assert not self._started, "Add all columns before setting slice"
		colname = _uni(colname)
		coltype = _uni(coltype)
		assert colname not in self.columns, colname
		assert colname
		typed_writer(coltype) # gives error for unknown types
		self.columns[colname] = (coltype, default)
		self._order.append(colname)
		if colname in self._pcolumns:
			self._clean_names[colname] = self._pcolumns[colname].name
		else:
			self._clean_names[colname] = _clean_name(colname, self._seen_n)

	def set_slice(self, sliceno):
		from g import running
		assert running != 'analysis', "Don't try to set_slice in analysis"
		self._set_slice(sliceno)

	def _set_slice(self, sliceno):
		assert self._started < 2, "Don't use both set_slice and a split writer"
		self.close()
		self.sliceno = sliceno
		writers = self._mkwriters(sliceno)
		if not self.meta_only:
			self.writers = writers
			self._mkwritefuncs()

	def column_filename(self, colname):
		return '%s/%d.%s' % (self.name, self.sliceno, self._clean_names[colname],)

	def _mkwriters(self, sliceno, filtered=True):
		assert self.columns, "No columns in dataset"
		if self.hashlabel:
			assert self.hashlabel in self.columns, "Hashed column (%s) missing" % (self.hashlabel,)
		self._started = 2 - filtered
		if self.meta_only:
			return
		writers = {}
		for colname, (coltype, default) in self.columns.items():
			wt = typed_writer(coltype)
			kw = {} if default is _nodefault else {'default': default}
			fn = self.column_filename(colname)
			if filtered and colname == self.hashlabel:
				from g import SLICES
				w = wt(fn, hashfilter=(sliceno, SLICES), **kw)
				self.hashcheck = w.hashcheck
			else:
				w = wt(fn, **kw)
			writers[colname] = w
		return writers

	def _mkwritefuncs(self):
		hl = self.hashlabel
		w_l = [self.writers[c].write for c in self._order]
		w = {k: w.write for k, w in self.writers.items()}
		if hl:
			hw = w.pop(hl)
			w_i = w.items()
			def write_dict(values):
				if hw(values[hl]):
					for k, w in w_i:
						w(values[k])
			self.write_dict = write_dict
			hix = self._order.index(hl)
			check = self.hashcheck
			def write_list(values):
				if check(values[hix]):
					for w, v in zip(w_l, values):
						w(v)
			self.write_list = write_list
		else:
			w_i = w.items()
			def write_dict(values):
				for k, w in w_i:
					w(values[k])
			self.write_dict = write_dict
			def write_list(values):
				for w, v in zip(w_l, values):
					w(v)
			self.write_list = write_list
		w_d = {'w%d' % (ix,): w for ix, w in enumerate(w_l)}
		names = [self._clean_names[n] for n in self._order]
		f = ['def write(' + ', '.join(names) + '):']
		if len(names) == 1: # only the hashlabel, no check needed
			f.append(' w0(%s)' % tuple(names))
		else:
			if hl:
				f.append(' if w%d(%s):' % (hix, names[hix],))
			else:
				hix = -1
			for ix in range(len(names)):
				if ix != hix:
					f.append('  w%d(%s)' % (ix, names[ix],))
		exec '\n'.join(f) in w_d
		self.write = w_d['write']

	@property
	def _allwriters(self):
		if self._allwriters_:
			return self._allwriters_
		from g import SLICES
		self._allwriters_ = [self._mkwriters(sliceno, False) for sliceno in range(SLICES)]
		return self._allwriters_

	def get_split_write(self):
		return self._split or self._mksplit()['split']

	def get_split_write_list(self):
		return self._split_list or self._mksplit()['split_list']

	def get_split_write_dict(self):
		return self._split_dict or self._mksplit()['split_dict']

	def _mksplit(self):
		assert self._started != 1, "Don't use both a split writer and set_slice"
		w_d = {}
		names = [self._clean_names[n] for n in self._order]
		w_d['names'] = names
		def key(t):
			return self._order.index(t[0])
		def d2l(d):
			return [w.write for _, w in sorted(d.items(), key=key)]
		w_d['writers'] = [d2l(d) for d in self._allwriters]
		f_____ = ['def split(' + ', '.join(names) + '):']
		f_list = ['def split_list(v):']
		f_dict = ['def split_dict(d):']
		from g import SLICES
		hl = self.hashlabel
		if hl:
			w_d['h'] = self._allwriters[0][hl].hash
			f_____.append('w_l = writers[h(%s) %% %d]' % (hl, SLICES,))
			f_list.append('w_l = writers[h(v[%d]) %% %d]' % (self._order.index(hl), SLICES,))
			f_dict.append('w_l = writers[h(d[%r]) %% %d]' % (hl, SLICES,))
		else:
			from itertools import cycle
			w_d['c'] = cycle(range(SLICES))
			f_____.append('w_l = writers[next(c)]')
			f_list.append('w_l = writers[next(c)]')
			f_dict.append('w_l = writers[next(c)]')
		for ix in range(len(names)):
			f_____.append('w_l[%d](%s)' % (ix, names[ix],))
			f_list.append('w_l[%d](v[%d])' % (ix, ix,))
			f_dict.append('w_l[%d](d[%r])' % (ix, self._order[ix],))
		exec '\n '.join(f_____) in w_d
		exec '\n '.join(f_list) in w_d
		exec '\n '.join(f_dict) in w_d
		self._split = w_d['split']
		self._split_list = w_d['split_list']
		self._split_dict = w_d['split_dict']
		return w_d

	def close(self):
		if not hasattr(self, 'writers'):
			return
		lens = {}
		minmax = {}
		for k, w in self.writers.items():
			lens[k] = w.count
			minmax[k] = (w.min, w.max,)
			w.close()
		len_set = set(lens.values())
		assert len(len_set) == 1, "Not all columns have the same linecount in slice %d: %r" % (self.sliceno, lens)
		self._lens[self.sliceno] = len_set.pop()
		self._minmax[self.sliceno] = minmax
		del self.writers

	def set_lines(self, sliceno, count):
		assert self.meta_only, "Don't try to set lines for writers that actually write"
		self._lens[sliceno] = count

	def set_minmax(self, sliceno, minmax):
		assert self.meta_only, "Don't try to set minmax for writers that actually write"
		self._minmax[sliceno] = minmax

	def finish(self):
		"""Normally you don't need to call this, but if you want to
		pass yourself as a dataset to a subjob you need to call
		this first."""
		from g import running, SLICES
		assert running == self._running or running == 'synthesis', "Finish where you started or in synthesis"
		if self._started == 2:
			for sliceno, writers in enumerate(self._allwriters):
				self.sliceno = sliceno
				self.writers = writers
				self.close()
		else:
			self.close()
		assert len(self._lens) == SLICES, "Not all slices written, missing %r" % (set(range(SLICES)) - set(self._lens),)
		args = dict(
			columns={k: v[0].split(':')[-1] for k, v in self.columns.items()},
			filenames=self._clean_names,
			lines=self._lens,
			minmax=self._minmax,
			filename=self.filename,
			hashlabel=self.hashlabel,
			caption=self.caption,
			previous=self.previous,
			name=self.name,
		)
		if self.parent:
			res = Dataset(self.parent)
			res.append(hashlabel_override=self.hashlabel_override, **args)
		else:
			res = Dataset.new(**args)
		del _datasetwriters[self.name]
		return res

# Backward compat sadness.
# Supports some of the info API, none of the reading or writing API.
# I hope this is what is actually commonly used.
# Please don't use this in new code.
class dataset:
	def load(self, jobid):
		self.d = Dataset(jobid)

	def name_type_list(self):
		return self.d.columns.items()

	def name_type_dict(self):
		return self.d.columns

	def get_filename(self):
		return self.d.filename

	def get_hashlabel(self):
		return self.d.hashlabel

	def get_jobid(self):
		return self.d.jobid

	def get_caption(self):
		return self.d.caption

	def get_num_lines_per_split(self):
		return self.d.lines