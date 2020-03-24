# Copyright 1999-2020 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import numpy as np
import pandas as pd

from ... import opcodes as OperandDef
from ...utils import lazy_import, get_shuffle_input_keys_idxes
from ...operands import OperandStage
from ...serialize import ValueType, Int32Field, ListField, StringField, BoolField
from ...tensor.base.psrs import PSRSOperandMixin
from ..utils import standardize_range_index
from ..operands import DataFrameOperandMixin, DataFrameOperand, DataFrameShuffleProxy, \
    ObjectType, DataFrameMapReduceOperand


cudf = lazy_import('cudf', globals=globals())


class DataFramePSRSOperandMixin(DataFrameOperandMixin, PSRSOperandMixin):
    @classmethod
    def _collect_op_properties(cls, op):
        from .sort_values import DataFrameSortValues
        if isinstance(op, DataFrameSortValues):
            properties = dict(sort_type='sort_values', axis=op.axis, by=op.by, ascending=op.ascending,
                              inplace=op.inplace, na_position=op.na_position)
        else:
            properties = dict(sort_type='sort_index', axis=op.axis, level=op.level, ascending=op.ascending,
                              inplace=op.inplace, na_position=op.na_position, sort_remaining=op.sort_remaining)
        return properties

    @classmethod
    def local_sort_and_regular_sample(cls, op, in_data, axis_chunk_shape, axis_offsets, out_idx):
        # stage 1: local sort and regular samples collected
        sorted_chunks, indices_chunks, sampled_chunks = [], [], []
        for i in range(axis_chunk_shape):
            in_chunk = in_data.chunks[i]
            kind = None if op.psrs_kinds is None else op.psrs_kinds[0]
            chunk_op = DataFramePSRSSortRegularSample(kind=kind, n_partition=axis_chunk_shape,
                                                      object_type=op.object_type,
                                                      **cls._collect_op_properties(op))
            kws = []
            sort_shape = in_chunk.shape
            kws.append({'shape': sort_shape,
                        'index_value': in_chunk.index_value,
                        'index': in_chunk.index})
            if chunk_op.sort_type == 'sort_values':
                sampled_shape = (axis_chunk_shape, len(op.by)) if \
                    op.by else (axis_chunk_shape,)
            else:
                sampled_shape = (axis_chunk_shape, sort_shape[1]) if\
                    len(sort_shape) == 2 else (axis_chunk_shape,)
            kws.append({'shape': sampled_shape,
                        'index_value': in_chunk.index_value,
                        'index': (i,),
                        'type': 'regular_sampled'})
            if op.object_type == ObjectType.dataframe:
                kws[0].update({'columns_value': in_chunk.columns_value, 'dtypes': in_chunk.dtypes})
                kws[1].update({'columns_value': in_chunk.columns_value, 'dtypes': in_chunk.dtypes})
            else:
                kws[0].update(({'dtype': in_chunk.dtype, 'name': in_chunk.name}))
                kws[1].update({'dtype': in_chunk.dtype})

            chunks = chunk_op.new_chunks([in_chunk], kws=kws, output_limit=len(kws))
            sort_chunk, sampled_chunk = chunks
            sorted_chunks.append(sort_chunk)
            sampled_chunks.append(sampled_chunk)
        return sorted_chunks, indices_chunks, sampled_chunks

    @classmethod
    def concat_and_pivot(cls, op, axis_chunk_shape, out_idx, sorted_chunks, sampled_chunks):
        # stage 2: gather and merge samples, choose and broadcast p-1 pivots
        kind = None if op.psrs_kinds is None else op.psrs_kinds[1]
        concat_pivot_op = DataFramePSRSConcatPivot(kind=kind, n_partition=axis_chunk_shape,
                                                   object_type=op.object_type,
                                                   **cls._collect_op_properties(op))
        concat_pivot_shape = \
            sorted_chunks[0].shape[:op.axis] + (axis_chunk_shape - 1,) + \
            sorted_chunks[0].shape[op.axis + 1:]
        concat_pivot_index = out_idx[:op.axis] + (0,) + out_idx[op.axis:]
        concat_pivot_chunk = concat_pivot_op.new_chunk(sampled_chunks,
                                                       shape=concat_pivot_shape,
                                                       index=concat_pivot_index,
                                                       object_type=op.object_type)
        return concat_pivot_chunk

    @classmethod
    def partition_local_data(cls, op, axis_chunk_shape, sorted_chunks,
                             indices_chunks, concat_pivot_chunk):
        # stage 3: Local data is partitioned
        partition_chunks = []
        length = len(sorted_chunks)
        for i in range(length):
            chunk_inputs = [sorted_chunks[i], concat_pivot_chunk]
            partition_shuffle_map = DataFramePSRSShuffle(n_partition=axis_chunk_shape,
                                                         stage=OperandStage.map,
                                                         object_type=op.object_type,
                                                         **cls._collect_op_properties(op))
            kw = dict(shape=chunk_inputs[0].shape,
                      index=chunk_inputs[0].index,
                      index_value=chunk_inputs[0].index_value)
            if op.object_type == ObjectType.dataframe:
                kw.update(dict(columns_value=chunk_inputs[0].columns_value,
                               dtypes=chunk_inputs[0].dtypes))
            else:
                kw.update(dict(dtype=chunk_inputs[0].dtype, name=chunk_inputs[0].name))
            partition_chunk = partition_shuffle_map.new_chunk(chunk_inputs, **kw)
            partition_chunks.append(partition_chunk)
        return partition_chunks

    @classmethod
    def partition_merge_data(cls, op, need_align, return_value, partition_chunks, proxy_chunk):
        # stage 4: all *ith* classes are gathered and merged
        partition_sort_chunks, partition_indices_chunks, sort_info_chunks = [], [], []
        for i, partition_chunk in enumerate(partition_chunks):
            kind = None if op.psrs_kinds is None else op.psrs_kinds[2]
            partition_shuffle_reduce = DataFramePSRSShuffle(
                stage=OperandStage.reduce, kind=kind, shuffle_key=str(i),
                object_type=op.object_type, **cls._collect_op_properties(op))
            chunk_shape = list(partition_chunk.shape)
            chunk_shape[op.axis] = np.nan

            kw = dict(shape=tuple(chunk_shape), index=partition_chunk.index,
                      index_value=partition_chunk.index_value)
            if op.object_type == ObjectType.dataframe:
                kw.update(dict(columns_value=partition_chunk.columns_value,
                               dtypes=partition_chunk.dtypes))
            else:
                kw.update(dict(dtype=partition_chunk.dtype, name=partition_chunk.name))
            cs = partition_shuffle_reduce.new_chunks([proxy_chunk], **kw)

            partition_sort_chunks.append(cs[0])
        return partition_sort_chunks, partition_indices_chunks, sort_info_chunks

    @classmethod
    def _tile_psrs(cls, op, in_data):
        out = op.outputs[0]
        in_df, axis_chunk_shape, _, _ = cls.preprocess(op, in_data=in_data)

        # stage 1: local sort and regular samples collected
        sorted_chunks, _, sampled_chunks = cls.local_sort_and_regular_sample(
            op, in_df, axis_chunk_shape, None, None)

        # stage 2: gather and merge samples, choose and broadcast p-1 pivots
        concat_pivot_chunk = cls.concat_and_pivot(
            op, axis_chunk_shape, (), sorted_chunks, sampled_chunks)

        # stage 3: Local data is partitioned
        partition_chunks = cls.partition_local_data(
            op, axis_chunk_shape, sorted_chunks, None, concat_pivot_chunk)

        proxy_chunk = DataFrameShuffleProxy(object_type=op.object_type).new_chunk(
            partition_chunks, shape=())

        # stage 4: all *ith* classes are gathered and merged
        partition_sort_chunks = cls.partition_merge_data(
            op, False, None, partition_chunks, proxy_chunk)[0]

        if op.ignore_index:
            chunks = standardize_range_index(partition_sort_chunks, axis=op.axis)
        else:
            chunks = partition_sort_chunks

        if op.object_type == ObjectType.dataframe:
            nsplits = ((np.nan,) * len(chunks), (out.shape[1],))
            new_op = op.copy()
            return new_op.new_dataframes(op.inputs, shape=out.shape, chunks=chunks,
                                         nsplits=nsplits, index_value=out.index_value,
                                         columns_value=out.columns_value, dtypes=out.dtypes)
        else:
            nsplits = ((np.nan,) * len(chunks), )
            new_op = op.copy()
            return new_op.new_seriess(op.inputs, shape=out.shape, chunks=chunks,
                                      nsplits=nsplits, index_value=out.index_value,
                                      dtype=out.dtype, name=out.name)


def execute_sort_values(data, op, inplace=None):
    if inplace is None:
        inplace = op.inplace
    # ignore_index is new in Pandas version 1.0.0.
    ignore_index = getattr(op, 'ignore_index', False)
    if isinstance(data, (pd.DataFrame, pd.Series)):
        kwargs = dict(axis=op.axis, ascending=op.ascending, ignore_index=ignore_index,
                      na_position=op.na_position, kind=op.kind)
        if isinstance(data, pd.DataFrame):
            kwargs['by'] = op.by
        if inplace:
            kwargs['inplace'] = True
            try:
                data.sort_values(**kwargs)
            except TypeError:  # pragma: no cover
                kwargs.pop('ignore_index', None)
                data.sort_values(**kwargs)
            return data
        else:
            try:
                return data.sort_values(**kwargs)
            except TypeError:  # pragma: no cover
                kwargs.pop('ignore_index', None)
                return data.sort_values(**kwargs)

    else:  # pragma: no cover
        # cudf doesn't support axis and kind
        if isinstance(data, cudf.DataFrame):
            return data.sort_values(
                op.by, ascending=op.ascending, na_position=op.na_position)
        else:
            return data.sort_values(
                ascending=op.ascending, na_position=op.na_position)


def execute_sort_index(data, op, inplace=None):
    if inplace is None:
        inplace = op.inplace
    # ignore_index is new in Pandas version 1.0.0.
    ignore_index = getattr(op, 'ignore_index', False)
    if isinstance(data, (pd.DataFrame, pd.Series)):
        kwargs = dict(level=op.level, ascending=op.ascending, ignore_index=ignore_index,
                      na_position=op.na_position, kind=op.kind, sort_remaining=op.sort_remaining)
        if inplace:
            kwargs['inplace'] = True
            try:
                data.sort_index(**kwargs)
            except TypeError:  # pragma: no cover
                kwargs.pop('ignore_index', None)
                data.sort_index(**kwargs)
            return data
        else:
            try:
                return data.sort_index(**kwargs)
            except TypeError:  # pragma: no cover
                kwargs.pop('ignore_index', None)
                return data.sort_index(**kwargs)

    else:  # pragma: no cover
        # cudf only support ascending
        return data.sort_index(ascending=op.ascending)


class DataFramePSRSChunkOperand(DataFrameOperand):
    # sort type could be 'sort_values' or 'sort_index'
    _sort_type = StringField('sort_type')

    _axis = Int32Field('axis')
    _by = ListField('by', ValueType.string)
    _ascending = BoolField('ascending')
    _inplace = BoolField('inplace')
    _kind = StringField('kind')
    _na_position = StringField('na_position')

    # for sort_index
    _level = ListField('level')
    _sort_remaining = BoolField('sort_remaining')

    _n_partition = Int32Field('n_partition')

    def __init__(self, sort_type=None, by=None, axis=None, ascending=None, inplace=None, kind=None,
                 na_position=None, level=None, sort_remaining=None, n_partition=None, object_type=None, **kw):
        super().__init__(_sort_type=sort_type, _by=by, _axis=axis, _ascending=ascending,
                         _inplace=inplace, _kind=kind, _na_position=na_position,
                         _level=level, _sort_remaining=sort_remaining, _n_partition=n_partition,
                         _object_type=object_type, **kw)

    @property
    def sort_type(self):
        return self._sort_type

    @property
    def axis(self):
        return self._axis

    @property
    def by(self):
        return self._by

    @property
    def ascending(self):
        return self._ascending

    @property
    def inplace(self):
        return self._inplace

    @property
    def kind(self):
        return self._kind

    @property
    def na_position(self):
        return self._na_position

    @property
    def level(self):
        return self._level

    @property
    def sort_remaining(self):
        return self._sort_remaining

    @property
    def n_partition(self):
        return self._n_partition


class DataFramePSRSSortRegularSample(DataFramePSRSChunkOperand, DataFrameOperandMixin):
    _op_type_ = OperandDef.PSRS_SORT_REGULAR_SMAPLE

    @property
    def output_limit(self):
        return 2

    @classmethod
    def execute(cls, ctx, op):
        a = ctx[op.inputs[0].key]

        n = op.n_partition
        w = int(a.shape[op.axis] // n)

        slc = (slice(None),) * op.axis + (slice(0, n * w, w),)
        if op.sort_type == 'sort_values':
            ctx[op.outputs[0].key] = res = execute_sort_values(a, op)
            # do regular sample
            if op.by is not None:
                ctx[op.outputs[-1].key] = res[op.by].iloc[slc]
            else:
                ctx[op.outputs[-1].key] = res.iloc[slc]
        else:
            ctx[op.outputs[0].key] = res = execute_sort_index(a, op)
            # do regular sample
            ctx[op.outputs[-1].key] = res.iloc[slc]


class DataFramePSRSConcatPivot(DataFramePSRSChunkOperand, DataFrameOperandMixin):
    _op_type_ = OperandDef.PSRS_CONCAT_PIVOT

    @property
    def output_limit(self):
        return 1

    @classmethod
    def execute(cls, ctx, op):
        inputs = [ctx[c.key] for c in op.inputs]
        xdf = pd if isinstance(inputs[0], (pd.DataFrame, pd.Series)) else cudf

        a = xdf.concat(inputs, axis=op.axis)
        p = len(inputs)
        assert a.shape[op.axis] == p ** 2

        select = slice(p - 1, (p - 1) ** 2 + 1, p - 1)
        slc = (slice(None),) * op.axis + (select,)
        if op.sort_type == 'sort_values':
            a = execute_sort_values(a, op, inplace=False)
            ctx[op.outputs[-1].key] = a.iloc[slc]
        else:
            a = execute_sort_index(a, op, inplace=False)
            ctx[op.outputs[-1].key] = a.index[slc]


class DataFramePSRSShuffle(DataFrameMapReduceOperand, DataFrameOperandMixin):
    _op_type_ = OperandDef.PSRS_SHUFFLE

    _sort_type = StringField('sort_type')

    # for shuffle map
    _axis = Int32Field('axis')
    _by = ListField('by', ValueType.string)
    _ascending = BoolField('ascending')
    _inplace = BoolField('inplace')
    _na_position = StringField('na_position')
    _n_partition = Int32Field('n_partition')

    # for sort_index
    _level = ListField('level')
    _sort_remaining = BoolField('sort_remaining')

    # for shuffle reduce
    _kind = StringField('kind')

    def __init__(self, sort_type=None, by=None, axis=None, ascending=None, n_partition=None,
                 na_position=None, inplace=None, kind=None, level=None, sort_remaining=None,
                 stage=None, shuffle_key=None, object_type=None, **kw):
        super().__init__(_sort_type=sort_type, _by=by, _axis=axis, _ascending=ascending,
                         _n_partition=n_partition, _na_position=na_position, _inplace=inplace,
                         _kind=kind, _level=level, _sort_remaining=sort_remaining, _stage=stage,
                         _shuffle_key=shuffle_key, _object_type=object_type, **kw)

    @property
    def sort_type(self):
        return self._sort_type

    @property
    def by(self):
        return self._by

    @property
    def axis(self):
        return self._axis

    @property
    def ascending(self):
        return self._ascending

    @property
    def inplace(self):
        return self._inplace

    @property
    def na_position(self):
        return self._na_position

    @property
    def level(self):
        return self._level

    @property
    def sort_remaining(self):
        return self._sort_remaining

    @property
    def n_partition(self):
        return self._n_partition

    @property
    def kind(self):
        return self._kind

    @property
    def output_limit(self):
        return 1

    @classmethod
    def _execute_dataframe_map(cls, ctx, op):
        a, pivots = [ctx[c.key] for c in op.inputs]
        out = op.outputs[0]

        if isinstance(a, pd.DataFrame):
            # use numpy.searchsorted to find split positions.
            records = a[op.by].to_records(index=False)
            p_records = pivots.to_records(index=False)
            if op.ascending:
                poses = records.searchsorted(p_records, side='right')
            else:
                poses = len(records) - records[::-1].searchsorted(p_records, side='right')

            poses = (None,) + tuple(poses) + (None,)
            for i in range(op.n_partition):
                values = a.iloc[poses[i]: poses[i + 1]]
                ctx[(out.key, str(i))] = values
        else:  # pragma: no cover
            # for cudf, find split positions in loops.
            if op.ascending:
                pivots.append(a.iloc[-1][op.by])
                for i in range(op.n_partition):
                    selected = a
                    for label in op.by:
                        selected = selected.loc[a[label] <= pivots.iloc[i][label]]
                    ctx[(out.key, str(i))] = selected
            else:
                pivots.append(a.iloc[-1][op.by])
                for i in range(op.n_partition):
                    selected = a
                    for label in op.by:
                        selected = selected.loc[a[label] >= pivots.iloc[i][label]]
                    ctx[(out.key, str(i))] = selected

    @classmethod
    def _execute_series_map(cls, ctx, op):
        a, pivots = [ctx[c.key] for c in op.inputs]
        out = op.outputs[0]

        if isinstance(a, pd.Series):
            if op.ascending:
                poses = a.searchsorted(pivots, side='right')
            else:
                poses = len(a) - a.iloc[::-1].searchsorted(pivots, side='right')
            poses = (None,) + tuple(poses) + (None,)
            for i in range(op.n_partition):
                values = a.iloc[poses[i]: poses[i + 1]]
                ctx[(out.key, str(i))] = values

    @classmethod
    def _execute_sort_index_map(cls, ctx, op):
        a, pivots = [ctx[c.key] for c in op.inputs]
        out = op.outputs[0]

        if op.ascending:
            poses = a.index.searchsorted(list(pivots), side='right')
        else:
            poses = len(a) - a.index[::-1].searchsorted(list(pivots), side='right')
        poses = (None,) + tuple(poses) + (None,)
        for i in range(op.n_partition):
            values = a.iloc[poses[i]: poses[i + 1]]
            ctx[(out.key, str(i))] = values

    @classmethod
    def _execute_map(cls, ctx, op):
        a = [ctx[c.key] for c in op.inputs][0]
        if op.sort_type == 'sort_values':
            if len(a.shape) == 2:
                # DataFrame type
                cls._execute_dataframe_map(ctx, op)
            else:
                # Series type
                cls._execute_series_map(ctx, op)
        else:
            cls._execute_sort_index_map(ctx, op)

    @classmethod
    def _execute_reduce(cls, ctx, op):
        input_keys, _ = get_shuffle_input_keys_idxes(op.inputs[0])
        raw_inputs = [ctx[(input_key, op.shuffle_key)] for input_key in input_keys]
        xdf = pd if isinstance(raw_inputs[0], (pd.DataFrame, pd.Series)) else cudf
        concat_values = xdf.concat(raw_inputs, axis=op.axis)
        if op.sort_type == 'sort_values':
            ctx[op.outputs[0].key] = execute_sort_values(concat_values, op)
        else:
            ctx[op.outputs[0].key] = execute_sort_index(concat_values, op)

    @classmethod
    def execute(cls, ctx, op):
        if op.stage == OperandStage.map:
            cls._execute_map(ctx, op)
        else:
            cls._execute_reduce(ctx, op)