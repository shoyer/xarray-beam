# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Rechunking for xarray.Dataset objets."""
import collections
import itertools
import logging
from typing import (
    Any, Dict, Iterable, Iterator, List, Optional, Mapping, Tuple, Union
)
import textwrap

import apache_beam as beam
import dataclasses
import numpy as np
from rechunker import algorithm
import xarray

from xarray_beam._src import core


# pylint: disable=logging-not-lazy
# pylint: disable=logging-format-interpolation


def normalize_chunks(
    chunks: Mapping[str, Union[int, Tuple[int, ...]]],
    dim_sizes: Mapping[str, int],
) -> Dict[str, int]:
  """Normalize a dict of chunks."""
  if not chunks.keys() <= dim_sizes.keys():
    raise ValueError(
        'all dimensions used in chunks must also have an indicated size: '
        f'chunks={chunks} vs dim_sizes={dim_sizes}')
  result = {}
  for dim, size in dim_sizes.items():
    if dim not in chunks:
      result[dim] = size
    elif isinstance(chunks[dim], tuple):
      unique_chunks = set(chunks[dim])
      if len(unique_chunks) != 1:
        raise ValueError(
            f'chunks for dimension {dim} are not constant: {unique_chunks}',
        )
      result[dim], = unique_chunks
    elif chunks[dim] == -1:
      result[dim] = size
    else:
      result[dim] = chunks[dim]
  return result


def rechunking_plan(
    dim_sizes: Mapping[str, int],
    source_chunks: Mapping[str, int],
    target_chunks: Mapping[str, int],
    itemsize: int,
    max_mem: int,
) -> List[Dict[str, int]]:
  """Make a rechunking plan."""
  plan_shapes = algorithm.rechunking_plan(
      shape=tuple(dim_sizes.values()),
      source_chunks=tuple(source_chunks[dim] for dim in dim_sizes),
      target_chunks=tuple(target_chunks[dim] for dim in dim_sizes),
      itemsize=itemsize,
      max_mem=max_mem,
  )
  return [dict(zip(dim_sizes.keys(), shapes)) for shapes in plan_shapes]


def _round_chunk_key(
    chunk_key: core.ChunkKey,
    target_chunks: Mapping[str, int],
) -> core.ChunkKey:
  """Round down a chunk-key to offsets corresponding to new chunks."""
  new_offsets = {}
  for dim, offset in chunk_key.items():
    chunk_size = target_chunks.get(dim)
    if chunk_size is None:
      new_offsets[dim] = offset
    elif chunk_size == -1:
      new_offsets[dim] = 0
    else:
      new_offsets[dim] = chunk_size * (offset // chunk_size)
  return core.ChunkKey(new_offsets)


def consolidate_chunks(
    inputs: Iterable[Tuple[core.ChunkKey, xarray.Dataset]],
    combine_kwargs: Optional[Mapping[str, Any]] = None,
) -> Tuple[core.ChunkKey, xarray.Dataset]:
  """Combine chunks into a single (ChunkKey, Dataset) pair."""
  inputs = list(inputs)
  keys = [key for key, _ in inputs]
  if len(set(keys)) < len(keys):
    raise ValueError(f'chunk keys are not unique: {keys}')

  # Reconstruct shared offsets along each dimension by inspecting chunk keys.
  unique_offsets = collections.defaultdict(set)
  for key in keys:
    for dim, offset in key.items():
      unique_offsets[dim].add(offset)
  offsets = {k: sorted(v) for k, v in unique_offsets.items()}
  combined_key = core.ChunkKey({k: v[0] for k, v in offsets.items()})

  # Consolidate inputs in a single xarray.Dataset.
  # `inputs` is a flat list like `[(k_00, ds_00), (k_01, ds_01), ...]` where
  # `k_ij` is a ChunkKey giving the (multi-dimensional) index of `ds_ij` in a
  # virtual larger Dataset.
  # Now we want to actually concatenate along all those dimensions, e.g., the
  # equivalent of building a large matrix out of sub-matrices:
  #       ⌈[x_00 x_01] ...⌉   ⌈x_00 x_01 ...⌉
  #   X = |[x_10 x_11] ...| = |x_10 x_11 ...|
  #       |[x_20 x_21] ...|   |x_20 x_21 ...|
  #       ⌊    ...     ...⌋   ⌊ ...  ... ...⌋
  # In NumPy, this would be done with `np.block()`.
  offset_index = core.compute_offset_index(offsets)
  shape = [len(v) for v in offsets.values()]
  if np.prod(shape) != len(inputs):
    raise ValueError('some expected chunk keys are missing')
  nested_array = np.empty(dtype=object, shape=shape)
  for key, chunk in inputs:
    nested_key = tuple(offset_index[dim][key[dim]] for dim in offsets)
    assert nested_array[nested_key] is None
    nested_array[nested_key] = chunk

  kwargs = dict(
      data_vars='minimal',
      coords='minimal',
      join='exact',
      combine_attrs='override',
  )
  if combine_kwargs is not None:
    kwargs.update(combine_kwargs)

  try:
    combined_dataset = xarray.combine_nested(
        nested_array.tolist(),
        concat_dim=list(offsets),
        **kwargs
    )
  except (ValueError, xarray.MergeError) as original_error:
    summaries = []
    for axis, dim in enumerate(offsets):
      repr_string = '\n'.join(
          repr(ds) for ds in nested_array[(0,) * axis + (slice(2),)].tolist()
      )
      if nested_array.shape[axis] > 2:
        repr_string += '\n...'
      repr_string = textwrap.indent(repr_string, prefix='  ')
      summaries.append(
          f'Leading datasets along dimension {dim!r}:\n{repr_string}'
      )
    summaries_str = '\n'.join(summaries)
    raise ValueError(
        f'combining nested dataset chunks with offsets {offsets} failed.\n'
        + summaries_str
    ) from original_error
  return combined_key, combined_dataset


@dataclasses.dataclass
class ConsolidateChunks(beam.PTransform):
  """Consolidate existing chunks into bigger chunks."""
  target_chunks: Mapping[str, int]

  def _prepend_chunk_key(self, key, chunk):
    rechunk_key = _round_chunk_key(key, self.target_chunks)
    return rechunk_key, (key, chunk)

  def _consolidate_chunks(self, key, inputs):
    consolidated_key, dataset = consolidate_chunks(inputs)
    assert key == consolidated_key, (key, consolidated_key)
    return consolidated_key, dataset

  def expand(self, pcoll):
    return (
        pcoll
        | 'PrependTempKey' >> beam.MapTuple(self._prepend_chunk_key)
        | 'GroupByTempKeys' >> beam.GroupByKey()
        | 'Consolidate' >> beam.MapTuple(self._consolidate_chunks)
    )


def _split_chunk_bounds(
    start: int, stop: int, multiple: int,
) -> List[Tuple[int, int]]:
  # pylint: disable=g-doc-args
  # pylint: disable=g-doc-return-or-yield
  """Calculate the size of divided chunks along a dimension.

  Example usage:

    >>> _split_chunk_bounds(0, 10, 3)
    [(0, 3), (3, 6), (6, 9), (9, 10)]
    >>> _split_chunk_bounds(5, 10, 3)
    [(5, 6), (6, 9), (9, 10)]
    >>> _split_chunk_bounds(10, 20, 12)
    [(10, 12), (12, 20)]
  """
  if multiple == -1:
    return [(start, stop)]
  assert start >= 0 and stop > start and multiple > 0, (start, stop, multiple)
  first_multiple = (start // multiple + 1) * multiple
  breaks = list(range(first_multiple, stop, multiple))
  return list(zip([start] + breaks, breaks + [stop]))


def split_chunks(
    key: core.ChunkKey,
    dataset: xarray.Dataset,
    target_chunks: Mapping[str, int],
) -> Iterator[Tuple[core.ChunkKey, xarray.Dataset]]:
  """Split a single (ChunkKey, xarray.Dataset) pair into many chunks."""
  # This function splits consolidated arrays into blocks of new sizes, e.g.,
  #       ⌈x_00 x_01 ...⌉   ⌈⌈x_00⌉ ⌈x_01⌉ ...⌉
  #   X = |x_10 x_11 ...| = ||x_10| |x_11| ...|
  #       |x_20 x_21 ...|   |⌊x_20⌋ ⌊x_21⌋ ...|
  #       ⌊ ... ...  ...⌋   ⌊  ...    ...  ...⌋
  # and emits them as (ChunkKey, xarray.Dataset) pairs.
  all_bounds = []
  for dim, chunk_size in target_chunks.items():
    start = key.get(dim, 0)
    stop = start + dataset.sizes[dim]
    all_bounds.append(_split_chunk_bounds(start, stop, chunk_size))

  for bounds in itertools.product(*all_bounds):
    offsets = dict(key)
    slices = {}
    for dim, (start, stop) in zip(target_chunks, bounds):
      base = key.get(dim, 0)
      offsets[dim] = start
      slices[dim] = slice(start - base, stop - base)

    new_key = core.ChunkKey(offsets)
    new_chunk = dataset.isel(slices)
    yield new_key, new_chunk


@dataclasses.dataclass
class SplitChunks(beam.PTransform):
  """Split existing chunks into smaller chunks."""
  target_chunks: Mapping[str, int]

  def _split_chunks(self, key, dataset):
    yield from split_chunks(key, dataset, self.target_chunks)

  def expand(self, pcoll):
    return pcoll | beam.FlatMapTuple(self._split_chunks)


def in_memory_rechunk(
    inputs: List[Tuple[core.ChunkKey, xarray.Dataset]],
    target_chunks: Mapping[str, int],
) -> Iterator[Tuple[core.ChunkKey, xarray.Dataset]]:
  """Rechunk in-memory pairs of (ChunkKey, xarray.Dataset)."""
  key, dataset = consolidate_chunks(inputs)
  yield from split_chunks(key, dataset, target_chunks)


@dataclasses.dataclass
class RechunkStage(beam.PTransform):
  """A single stage of a rechunking pipeline."""
  source_chunks: Mapping[str, int]
  target_chunks: Mapping[str, int]

  def expand(self, pcoll):
    source_values = self.source_chunks.values()
    target_values = self.target_chunks.values()
    if any(t % s for s, t in zip(source_values, target_values)):
      pcoll |= 'Split' >> SplitChunks(self.target_chunks)
    if any(s % t for s, t in zip(source_values, target_values)):
      pcoll |= 'Consolidate' >> ConsolidateChunks(self.target_chunks)
    return pcoll


class Rechunk(beam.PTransform):
  """Rechunk to an arbitrary new chunking scheme with bounded memory usage.

  The approach taken here builds on Rechunker [1], but differs in two key ways:
  1. It is performed via collective Beam operations, instead of writing
     intermediates arrays to disk.
  2. It is performed collectively on full xarray.Dataset objects, instead of
     NumPy arrays.

  [1] rechunker.readthedocs.io
  """

  def __init__(
      self,
      dim_sizes: Mapping[str, int],
      source_chunks: Mapping[str, Union[int, Tuple[int, ...]]],
      target_chunks: Mapping[str, Union[int, Tuple[int, ...]]],
      itemsize: int,
      max_mem: int = 2**30,  # 1 GB
  ):
    """Initialize Rechunk().

    Args:
      dim_sizes: size of the full (combined) dataset of all chunks.
      source_chunks: sizes of source chunks. Missing keys or values equal to -1
        indicate "non-chunked" dimensions.
      target_chunks: sizes of target chunks, like `source_keys`. Keys must
        exactly match those found in source_chunks.
      itemsize: approximate number of bytes per xarray.Dataset element,
        after indexing out by all dimensions, e.g., `4 * len(dataset)` for
        float32 data or roughly `dataset.nbytes / np.prod(dataset.sizes)`.
      max_mem: maximum memory that a single intermediate chunk may consume.
    """
    if source_chunks.keys() != target_chunks.keys():
      raise ValueError(
          f'source_chunks and target_chunks have different keys: '
          f'{source_chunks} vs {target_chunks}'
      )
    self.dim_sizes = dim_sizes
    self.source_chunks = normalize_chunks(source_chunks, dim_sizes)
    self.target_chunks = normalize_chunks(target_chunks, dim_sizes)
    plan = rechunking_plan(
        dim_sizes,
        self.source_chunks,
        self.target_chunks,
        itemsize=itemsize,
        max_mem=max_mem,
    )
    self.read_chunks, self.intermediate_chunks, self.write_chunks = plan

    # TODO(shoyer): multi-stage rechunking, when supported by rechunker:
    # https://github.com/pangeo-data/rechunker/pull/89
    self.stage_in = [self.source_chunks, self.read_chunks, self.write_chunks]
    self.stage_out = [self.read_chunks, self.write_chunks, self.target_chunks]
    logging.info(
        'Rechunking plan:\n' +
        '\n'.join(f'{s} -> {t}' for s, t in zip(self.stage_in, self.stage_out))
    )
    min_size = itemsize * np.prod(list(self.intermediate_chunks.values()))
    logging.info(f'Smallest intermediates have size {min_size:1.3e}')

  def expand(self, pcoll):
    # TODO(shoyer): consider splitting xarray.Dataset objects into separate
    # arrays for rechunking, which is more similar to what Rechunker does and
    # in principle could be more efficient.
    for stage, (in_chunks, out_chunks) in enumerate(
        zip(self.stage_in, self.stage_out)
    ):
      pcoll |= f'Stage{stage}' >> RechunkStage(in_chunks, out_chunks)
    return pcoll
