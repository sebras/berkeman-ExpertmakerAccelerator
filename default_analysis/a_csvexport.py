from __future__ import division

from extras import OptionString, job_params
from gzwrite import GzWrite

options = dict(
	filename          = OptionString, # .csv or .gz
	separator         = ',',
	labelsonfirstline = True,
	chain_source      = False, # everything in source is replaced by datasetchain(self, stop=from previous)
	quote_fields      = '', # can be ' or "
	labels            = [], # empty means all labels in (first) dataset
	sliced            = False, # one output file per slice, put %02d or similar in filename
)

datasets = (['source'],) # normally just one, but you can specify several

jobids = ('previous',)

def csvexport(sliceno, filename):
	assert len(options.separator) == 1
	assert options.quote_fields in ('', "'", '"',)
	d = datasets.source[0]
	if not options.labels:
		options.labels = sorted(d.columns)
	if options.chain_source:
		if jobids.previous:
			prev_source = job_params(jobids.previous).datasets.source
			assert len(datasets.source) == len(prev_source)
		else:
			prev_source = [None] * len(datasets.source)
		lst = []
		for src, stop in zip(datasets.source, prev_source):
			lst.extend(src.chain(stop_jobid=stop))
		datasets.source = lst
	if filename.lower().endswith('.gz'):
		mkwrite = GzWrite
	elif filename.lower().endswith('.csv'):
		def mkwrite(filename):
			return open(filename, "wb")
	else:
		raise Exception("Filename should end with .gz for compressed or .csv for uncompressed")
	it = d.iterate_list(sliceno, options.labels, datasets.source)
	with mkwrite(filename) as fh:
		q = options.quote_fields
		sep = options.separator
		if q:
			qq = q + q
			if options.labelsonfirstline:
				fh.write(sep.join(q + n.replace(q, qq) + q for n in options.labels) + '\n')
			for data in it:
				fh.write(sep.join(q + str(n).replace(q, qq) + q for n in data) + '\n')
		else:
			if options.labelsonfirstline:
				fh.write(sep.join(options.labels) + '\n')
			for data in it:
				fh.write(sep.join(map(str, data)) + '\n')

def analysis(sliceno):
	if options.sliced:
		csvexport(sliceno, options.filename % (sliceno,))

def synthesis():
	if not options.sliced:
		csvexport(None, options.filename)