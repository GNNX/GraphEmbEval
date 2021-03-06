#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
:Description: Implementation of the NVC (Node Vectors by Clusters) parser
	NVC format is a compact readable format for the graph/network embedding vectors.
:Authors: Artem Lutov <luart@ya.ru>
:Organizations: eXascale lab <http://exascale.info/>, Lumais <http://www.lumais.com/>
:Date: 2019-03
"""
from __future__ import print_function, division  # Required for stderr output, must be the first import
from scipy.sparse import dok_matrix  #, coo_matrix
from math import sqrt
import numpy as np
import sys


def loadNvc(nvcfile):
	"""Load network embeddings from the specified file in the NVC format v1.2.1

	nvcfile: str  - file name

	return
		embeds: matrix(float32)  - embeddings matrix in the Dictionary Of Keys sparse matrix format of the shape (ndsnum, dimnum)
		rootdims: array(uint16)  - indicies of the root dimensions
		dimrds: array(float32)  - ratios of dimension (cluster) density step relative to the possibly indirect super cluster, typically >= 1
		dimrws: array(float32)  - ratios of dimension (cluster) weight step relative to the possibly indirect super cluster, typically <= 1
		dimwsim: array(float32)  - dimensions weights for the similarity or None
		dimwdis: array(float32)  - dimensions weights for the dissimilarity or None
		dimnds: array(uint32)  - dimensions members (nodes) number
	"""
	hdr = False  # Whether the header is parsed
	ftr = False # Whether the footer is parsed
	ndsnum = 0  # The number of nodes
	dimnum = 0  # The number of dimensions (reprsentative clusters)
	rootdnum = 0  # The number of root dimensions (reprsentative clusters)
	numbered = False
	rootdims = None  # Indices of the root dimensions
	COMPR_NONE = 0
	COMPR_RLE = 1
	COMPR_SPARSE = 2
	COMPR_CLUSTER = 4  # Default
	compr = COMPR_CLUSTER  # Compression type
	VAL_BIT = 0
	VAL_UINT8 = 1
	VAL_UINT16 = 2
	VAL_FLOAT32 = 4
	valfmt = VAL_UINT8  # Falue format
	hdrvals = {'nodes:': None, 'dimensions:': None, 'rootdims:': None, 'value:': None, 'compression:': None, 'valmin:': None, 'numbered:': None}
	assert not any(filter(lambda k: not k.endswith(':'), hdrvals)), 'Header attribute names must end with ":"'
	irow = 0  # Payload line (matrix row) index (of either dimensions or nodes)
	nvec = None  # Node vectors matrix
	vqmax = np.uint16(0xFF if valfmt == VAL_UINT8 else 0xFFFF)  # Maximal (and normalization) value for the vector quantification based compression
	valmin = 0  # Minimal value (bottom margin) accounted in the node vectors, affects interpretation of the encoded values (format range)
	valcorr = 0  # Value correction caused by valmin

	def vqdec(v):
		"""Vector quantified value decoding (decompression)

		v: str(np.uint16)  - a string value to be decoded, E [1, 255]

		return np.float32  - the resulting decoded value
		"""
		return valcorr + (1 - valcorr) * np.float32(vqmax - np.uint16(v) + 1) / vqmax

	try:
		ln = ''
		with open(nvcfile, 'r') as fnvc:
			for ln in fnvc:
				if not ln:
					continue
				if ln[0] == '#':
					if not hdr:
						# Parse the header
						# Consider ',' separator besides the space
						ln = ' '.join(ln[1:].split(','))
						toks = ln.split(None, len(hdrvals) * 2)
						while toks:
							#print('toks: ', toks)
							key = toks[0].lower()
							isep = 0 if key.endswith(':') else key.find(':') + 1
							if isep:
								val = key[isep:]
								key = key[:isep]
							if key not in hdrvals:
								break
							hdr = True
							if isep:
								hdrvals[key] = val
								toks = toks[1:]
							elif len(toks) >= 2:
								hdrvals[key] = toks[1]
								toks = toks[2:]
							else:
								del toks[:]
						if hdr:
							#print('hdrvals:',hdrvals)
							ndsnum = np.uint32(hdrvals.get('nodes:', ndsnum))
							dimnum = np.uint16(hdrvals.get('dimensions:', dimnum))
							rootdnum = np.uint16(hdrvals.get('rootdims:', rootdnum))
							valmin = np.float32(hdrvals.get('valmin:', valmin))
							numbered = int(hdrvals.get('numbered:', numbered))  # Note: bool('0') is True (non-empty string)
							comprstr = hdrvals.get('compression:', '').lower()
							if comprstr == 'none':
								compr = COMPR_NONE
							elif comprstr == 'rle':
								compr = COMPR_RLE
							elif comprstr == 'sparse':
								compr = COMPR_SPARSE
							elif comprstr == 'cluster':
								compr = COMPR_CLUSTER
							else:
								raise ValueError('Unknown compression format: ' + compr)
							valstr = hdrvals.get('value:', '').lower()
							if valstr == 'bit':
								valfmt = VAL_BIT
							elif valstr == 'uint8':
								valfmt = VAL_UINT8
								valcorr = np.float32(max(valmin - 0.5 / vqmax, 0))
							elif valstr == 'uint16':
								valfmt = VAL_UINT16
								valcorr = np.float32(max(valmin - 0.5 / vqmax, 0))
							elif valstr == 'float32':
								valfmt = VAL_FLOAT32
							else:
								raise ValueError('Unknown value format: ' + valstr)
							#print('hdrvals:',hdrvals, '\nnumbered:', numbered)
					elif not ftr:
						# Parse the footer
						# [Diminfo> <cl0_id>[#<cl0_levid>][%<cl0_rdens>][/<cl0_rweight>][:<cl0_wsim>[-<cl0_wdis>]][!] ...
						# A possible value: 482716#2%1.16899/9.1268E-05:0.793701-0.114708
						vals = ln[1:].split(None, 1)
						if (not vals or not vals[0].lower().startswith('diminfo')
						or len(vals[0]) <= len('diminfo') or vals[0][len('diminfo')] not in '|>'):  # Diminfo| or Diminfo>
							continue
						levsnum = 0
						if vals[0][-1] == '|':
							vals = vals[1].split('>', 1)
							dimsopts = vals[0].strip().lower()
							hdrln = 'levsnum:'
							if dimsopts.startswith(hdrln):
								levsnum = int(dimsopts[len(hdrln):])
						ftr = True
						if len(vals) <= 1:
							continue
						vals = vals[1].split()
						# Initialize resulting arrays
						if rootdnum:
							rootdims = np.empty(rootdnum, np.uint16)

						# [#<cl0_levid>][%<cl0_rdens>][/<cl0_rweight>][:<cl0_wsim>[-<cl0_wdis>]][!]
						dimlev = None
						parts = []  # Parsing parts
						if dimnum:
							sep = '#'
							pos = vals[0].find(sep) + 1
							if pos != 0:
								dimlev = np.empty(dimnum, np.uint16)
								parts.append((sep, dimlev))
							dimhdrs = '%/:-'
							mwsim = ':'
							mwdis = '-'
							for sep in dimhdrs:  # dimrds, dimrws, dimwsim, dimwdis
								# Note: dimwdis is not parsed wihtout the dimwsim
								if sep == mwdis and parts[-1][1] is None:
									assert parts[-1][0] == mwsim, 'dimwdis should not be parsed wihtout the dimwsim'
									parts.append([sep, None])
									continue
								pos2 = vals[0].find(sep, pos) + 1
								if pos2 != 0:
									pos = pos2
									parts.append((sep, np.empty(dimnum, np.float32)))
								else:
									parts.append([sep, None])
							sep = '='
							pos = vals[0].find(sep, pos) + 1
							if pos != 0:
								parts.append((sep, np.empty(dimnum, np.uint32)))  # dimnds
								# ATTENTION: Evalaute only in case both dis and similarity have not been specifeid
								# to be consistent (the specified values could be evaluated differently)
								evalsims = parts[dimhdrs.index(mwsim)][1] is None and parts[dimhdrs.index(mwdis)][1] is None
								if evalsims:
									if not levsnum or dimlev is None:
										raise ValueError('levsnum and #levid should be specified in the Diminfo to evaluate dis/similarity')
									dimwsim = np.empty(dimnum, np.float32)
									parts[dimhdrs.index(mwsim)][1] = dimwsim
									dimwdis = np.empty(dimnum, np.float32)
									parts[dimhdrs.index(mwdis)][1] = dimwdis

						# Form indices of the root dims
						ird = 0  # Index in the rootdims array
						for iv, v in enumerate(vals):
							# Fetch root indices
							if v.endswith('!'):
								rootdims[ird] = iv
								ird += 1
								v = v[:-1]
							# Parse the fragment: [%<cl0_rdens>][/<cl0_rweight>][:<cl0_wsim>[-<cl0_wdis>]][=<nds_num>]
							ibeg = v.find(parts[0][0])
							for ipt, pt in enumerate(parts):
								if pt[1] is None:
									continue
								iptn = ipt + 1
								while iptn < len(parts) and parts[iptn][1] is None:
									iptn += 1
								iend = None if iptn == len(parts) else v.find(parts[iptn][0], ibeg + 1)
								assert iend is None or iend >= 1, 'iend is invalid'
								pt[1][iv] = v[ibeg + 1 : iend]
								if pt[0] == '=':
									ilev = dimlev[iv]
									if evalsims:
										dimwsim[iv] = pow(ilev, -1 / 3)  # desrank = ilev = pt[1][iv], ilev >= 1
										dimwdis[iv] = 1 / sqrt((levsnum - ilev) + 1)  # orank = levsnum - ilev
								ibeg = iend
						assert rootdims is None or ird == len(rootdims), ('Rootdims formation validation failed'
							', rootdims: {}, idr: {} / {}'.format(rootdims is not None, ird, len(rootdims)))
					continue

				# Construct the matrix
				if not (ndsnum and dimnum):
					raise ValueError('Invalid file header, the number of nodes ({}) and dimensions ({}) should be positive'.format(ndsnum, dimnum))
				# TODO: Ensure that non-float format for bits does not affect the subsequent evaluations or use dtype=np.float32 for all value formats
				if nvec is None:
					nvec = dok_matrix((ndsnum, dimnum), dtype=np.float32 if valfmt != VAL_BIT else np.uint8)

				# Parse the body
				if numbered:
					# Omit the cluster or node id prefix of each row
					ln = ln.split('>', 1)[1]
				vals = ln.split()
				if not vals:
					continue
				if compr == COMPR_CLUSTER:
					if valfmt == VAL_BIT:
						for nd in vals:
							nvec[np.uint32(nd), irow] = 1
					else:
						nids, vals = zip(*[v.split(':') for v in vals])
						if valfmt == VAL_UINT8 or valfmt == VAL_UINT16:
							# vals = [np.float32(1. / np.uint16(v)) for v in vals]
							vals = [vqdec(v) for v in vals]
						else:
							assert valfmt == VAL_FLOAT32, 'Unexpected valfmt'
						for i, nd in enumerate(nids):
							nvec[np.uint32(nd), irow] = vals[i]
				elif compr == COMPR_SPARSE:
					if valfmt == VAL_BIT:
						for dm in vals:
							nvec[irow, np.uint32(dm)] = 1
					else:
						dms, vals = zip(*[v.split(':') for v in vals])
						if valfmt == VAL_UINT8 or valfmt == VAL_UINT16:
							# vals = [np.float32(1. / np.uint16(v)) for v in vals]
							vals = [vqdec(v) for v in vals]
						else:
							assert valfmt == VAL_FLOAT32, 'Unexpected valfmt'
						for i, dm in enumerate(dms):
							nvec[irow, np.uint32(dm)] = vals[i]
				elif compr == COMPR_RLE:
					corr = 0  # RLE caused index correction
					for j, v in enumerate(vals):
						if v[0] != '0':
							if valfmt == VAL_UINT8 or valfmt == VAL_UINT16:
								# nvec[irow, j + corr] = 1. / np.uint16(v)
								nvec[irow, j + corr] = vqdec(v)
							else:
								assert valfmt == VAL_FLOAT32 or valfmt == VAL_BIT, 'Unexpected valfmt'
								nvec[irow, j + corr] = v
						elif len(v) >= 2:
							if v[1] != ':':
								raise ValueError('Invalid RLE value (":" separator is expected): ' + v);
							corr = np.uint16(v[2:]) + 1  # Length, the number of values to be inserted / skipped
						else:
							corr += 1
				else:
					assert compr == COMPR_NONE, 'Unexpected compression format'
					corr = 0  # 0 caused index correction
					for j, v in enumerate(vals):
						if v == '0':
							corr += 1
							continue
						if valfmt == VAL_UINT8 or valfmt == VAL_UINT16:
							# nvec[irow, j + corr] = 1. / np.uint16(v)
							nvec[irow, j + corr] = vqdec(v)
						else:
							assert valfmt == VAL_FLOAT32 or valfmt == VAL_BIT, 'Unexpected valfmt'
							nvec[irow, j + corr] = v
				irow += 1
	except Exception as err:
		print('ERROR, parsed row #{}, line:\n{}\n{}'.format(irow, ln, err), file=sys.stderr)
		raise

	assert not dimnum or dimnum == irow, 'The parsed number of dimensions is invalid'
	# Omit empty dimensions
	if dimnum != irow:
		dimnum == irow
	#print('nvec:\n', nvec, '\ndimwsim:\n', dimwsim, '\ndimwdis:\n', dimwdis)
	# Return node vecctors matrix in the Dictionary Of Keys based sparse format and dimension weights
	# assert len(parts) == 4, 'Parts should represent: dimrds, dimrws, dimwsim, dimwdis'
	# _, dimrds, dimrws, dimwsim, dimwdis, dimnds = (p[1] for p in parts)  # _ is dimlev
	parts.pop(0)  # Remove dimlev
	# Fill remained values with None (consider the deprecated format lacking dimnds)
	while len(parts) < 5:
		parts.append((None, None))
	dimrds, dimrws, dimwsim, dimwdis, dimnds = (p[1] for p in parts)
	assert dimwsim is None or dimwdis is None or len(dimwsim) == len(dimwdis), (
		'Parsed dimension weights are not synchronized')
	return nvec, rootdims, dimrds, dimrws, dimwsim, dimwdis, dimnds  # nvec.tocsc() - Compressed Sparse Column format
