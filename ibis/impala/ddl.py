# Copyright 2014 Cloudera Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json

import ibis.expr.datatypes as dt
from ibis.backends.base_sql import type_to_sql_string
from ibis.backends.base_sql.ddl import (
    BaseDDL,
    BaseQualifiedSQLStatement,
    CreateTable,
    CreateTableWithSchema,
    DropObject,
    DropTable,
    _sanitize_format,
    format_schema,
)
from ibis.backends.base_sqlalchemy.compiler import DML


class ImpalaDML(DML, BaseQualifiedSQLStatement):
    pass


def _serdeproperties(props):
    formatted_props = _format_properties(props)
    return 'SERDEPROPERTIES {}'.format(formatted_props)


def format_tblproperties(props):
    formatted_props = _format_properties(props)
    return 'TBLPROPERTIES {}'.format(formatted_props)


def _format_properties(props):
    tokens = []
    for k, v in sorted(props.items()):
        tokens.append("  '{}'='{}'".format(k, v))

    return '(\n{}\n)'.format(',\n'.join(tokens))


class CreateTableParquet(CreateTable):
    def __init__(
        self,
        table_name,
        path,
        example_file=None,
        example_table=None,
        schema=None,
        external=True,
        **kwargs,
    ):
        super().__init__(
            table_name,
            external=external,
            format='parquet',
            path=path,
            **kwargs,
        )
        self.example_file = example_file
        self.example_table = example_table
        self.schema = schema

    @property
    def _pieces(self):
        if self.example_file is not None:
            yield "LIKE PARQUET '{0}'".format(self.example_file)
        elif self.example_table is not None:
            yield "LIKE {0}".format(self.example_table)
        elif self.schema is not None:
            yield format_schema(self.schema)
        else:
            raise NotImplementedError

        yield self._storage()
        yield self._location()


class DelimitedFormat:
    def __init__(
        self,
        path,
        delimiter=None,
        escapechar=None,
        na_rep=None,
        lineterminator=None,
    ):
        self.path = path
        self.delimiter = delimiter
        self.escapechar = escapechar
        self.lineterminator = lineterminator
        self.na_rep = na_rep

    def to_ddl(self):
        yield 'ROW FORMAT DELIMITED'

        if self.delimiter is not None:
            yield "FIELDS TERMINATED BY '{}'".format(self.delimiter)

        if self.escapechar is not None:
            yield "ESCAPED BY '{}'".format(self.escapechar)

        if self.lineterminator is not None:
            yield "LINES TERMINATED BY '{}'".format(self.lineterminator)

        yield "LOCATION '{}'".format(self.path)

        if self.na_rep is not None:
            props = {'serialization.null.format': self.na_rep}
            yield format_tblproperties(props)


class AvroFormat:
    def __init__(self, path, avro_schema):
        self.path = path
        self.avro_schema = avro_schema

    def to_ddl(self):
        yield 'STORED AS AVRO'
        yield "LOCATION '{}'".format(self.path)

        schema = json.dumps(self.avro_schema, indent=2, sort_keys=True)
        schema = '\n'.join(x.rstrip() for x in schema.splitlines())

        props = {'avro.schema.literal': schema}
        yield format_tblproperties(props)


class ParquetFormat:
    def __init__(self, path):
        self.path = path

    def to_ddl(self):
        yield 'STORED AS PARQUET'
        yield "LOCATION '{}'".format(self.path)


class CreateTableDelimited(CreateTableWithSchema):
    def __init__(
        self,
        table_name,
        path,
        schema,
        delimiter=None,
        escapechar=None,
        lineterminator=None,
        na_rep=None,
        external=True,
        **kwargs,
    ):
        table_format = DelimitedFormat(
            path,
            delimiter=delimiter,
            escapechar=escapechar,
            lineterminator=lineterminator,
            na_rep=na_rep,
        )
        super().__init__(
            table_name, schema, table_format, external=external, **kwargs
        )


class CreateTableAvro(CreateTable):
    def __init__(self, table_name, path, avro_schema, external=True, **kwargs):
        super().__init__(table_name, external=external, **kwargs)
        self.table_format = AvroFormat(path, avro_schema)

    @property
    def _pieces(self):
        yield '\n'.join(self.table_format.to_ddl())


class InsertSelect(ImpalaDML):
    def __init__(
        self,
        table_name,
        select_expr,
        database=None,
        partition=None,
        partition_schema=None,
        overwrite=False,
    ):
        self.table_name = table_name
        self.database = database
        self.select = select_expr

        self.partition = partition
        self.partition_schema = partition_schema

        self.overwrite = overwrite

    def compile(self):
        if self.overwrite:
            cmd = 'INSERT OVERWRITE'
        else:
            cmd = 'INSERT INTO'

        if self.partition is not None:
            part = _format_partition(self.partition, self.partition_schema)
            partition = ' {} '.format(part)
        else:
            partition = ''

        select_query = self.select.compile()
        scoped_name = self._get_scoped_name(self.table_name, self.database)
        return '{0} {1}{2}\n{3}'.format(
            cmd, scoped_name, partition, select_query
        )


def _format_partition(partition, partition_schema):
    tokens = []
    if isinstance(partition, dict):
        for name in partition_schema:
            if name in partition:
                tok = _format_partition_kv(
                    name, partition[name], partition_schema[name]
                )
            else:
                # dynamic partitioning
                tok = name
            tokens.append(tok)
    else:
        for name, value in zip(partition_schema, partition):
            tok = _format_partition_kv(name, value, partition_schema[name])
            tokens.append(tok)

    return 'PARTITION ({})'.format(', '.join(tokens))


def _format_partition_kv(k, v, type):
    if type == dt.string:
        value_formatted = '"{}"'.format(v)
    else:
        value_formatted = str(v)

    return '{}={}'.format(k, value_formatted)


class LoadData(BaseDDL):

    """
    Generate DDL for LOAD DATA command. Cannot be cancelled
    """

    def __init__(
        self,
        table_name,
        path,
        database=None,
        partition=None,
        partition_schema=None,
        overwrite=False,
    ):
        self.table_name = table_name
        self.database = database
        self.path = path

        self.partition = partition
        self.partition_schema = partition_schema

        self.overwrite = overwrite

    def compile(self):
        overwrite = 'OVERWRITE ' if self.overwrite else ''

        if self.partition is not None:
            partition = '\n' + _format_partition(
                self.partition, self.partition_schema
            )
        else:
            partition = ''

        scoped_name = self._get_scoped_name(self.table_name, self.database)
        return "LOAD DATA INPATH '{}' {}INTO TABLE {}{}".format(
            self.path, overwrite, scoped_name, partition
        )


class AlterTable(BaseDDL):
    def __init__(
        self,
        table,
        location=None,
        format=None,
        tbl_properties=None,
        serde_properties=None,
    ):
        self.table = table
        self.location = location
        self.format = _sanitize_format(format)
        self.tbl_properties = tbl_properties
        self.serde_properties = serde_properties

    def _wrap_command(self, cmd):
        return 'ALTER TABLE {}'.format(cmd)

    def _format_properties(self, prefix=''):
        tokens = []

        if self.location is not None:
            tokens.append("LOCATION '{}'".format(self.location))

        if self.format is not None:
            tokens.append("FILEFORMAT {}".format(self.format))

        if self.tbl_properties is not None:
            tokens.append(format_tblproperties(self.tbl_properties))

        if self.serde_properties is not None:
            tokens.append(_serdeproperties(self.serde_properties))

        if len(tokens) > 0:
            return '\n{}{}'.format(prefix, '\n'.join(tokens))
        else:
            return ''

    def compile(self):
        props = self._format_properties()
        action = '{} SET {}'.format(self.table, props)
        return self._wrap_command(action)


class PartitionProperties(AlterTable):
    def __init__(
        self,
        table,
        partition,
        partition_schema,
        location=None,
        format=None,
        tbl_properties=None,
        serde_properties=None,
    ):
        super().__init__(
            table,
            location=location,
            format=format,
            tbl_properties=tbl_properties,
            serde_properties=serde_properties,
        )
        self.partition = partition
        self.partition_schema = partition_schema

    def _compile(self, cmd, property_prefix=''):
        part = _format_partition(self.partition, self.partition_schema)
        if cmd:
            part = '{} {}'.format(cmd, part)

        props = self._format_properties(property_prefix)
        action = '{} {}{}'.format(self.table, part, props)
        return self._wrap_command(action)


class AddPartition(PartitionProperties):
    def __init__(self, table, partition, partition_schema, location=None):
        super().__init__(table, partition, partition_schema, location=location)

    def compile(self):
        return self._compile('ADD')


class AlterPartition(PartitionProperties):
    def compile(self):
        return self._compile('', 'SET ')


class DropPartition(PartitionProperties):
    def __init__(self, table, partition, partition_schema):
        super().__init__(table, partition, partition_schema)

    def compile(self):
        return self._compile('DROP')


class RenameTable(AlterTable):
    def __init__(
        self, old_name, new_name, old_database=None, new_database=None
    ):
        # if either database is None, the name is assumed to be fully scoped
        self.old_name = old_name
        self.old_database = old_database
        self.new_name = new_name
        self.new_database = new_database

        new_qualified_name = new_name
        if new_database is not None:
            new_qualified_name = self._get_scoped_name(new_name, new_database)

        old_qualified_name = old_name
        if old_database is not None:
            old_qualified_name = self._get_scoped_name(old_name, old_database)

        self.old_qualified_name = old_qualified_name
        self.new_qualified_name = new_qualified_name

    def compile(self):
        cmd = '{} RENAME TO {}'.format(
            self.old_qualified_name, self.new_qualified_name
        )
        return self._wrap_command(cmd)


class TruncateTable(BaseDDL):

    _object_type = 'TABLE'

    def __init__(self, table_name, database=None):
        self.table_name = table_name
        self.database = database

    def compile(self):
        name = self._get_scoped_name(self.table_name, self.database)
        return 'TRUNCATE TABLE {}'.format(name)


class DropView(DropTable):

    _object_type = 'VIEW'


class CacheTable(BaseDDL):
    def __init__(self, table_name, database=None, pool='default'):
        self.table_name = table_name
        self.database = database
        self.pool = pool

    def compile(self):
        scoped_name = self._get_scoped_name(self.table_name, self.database)
        return "ALTER TABLE {} SET CACHED IN '{}'".format(
            scoped_name, self.pool
        )


class CreateFunction(BaseDDL):

    _object_type = 'FUNCTION'

    def __init__(self, func, name=None, database=None):
        self.func = func
        self.name = name or func.name
        self.database = database

    def _impala_signature(self):
        scoped_name = self._get_scoped_name(self.name, self.database)
        input_sig = _impala_input_signature(self.func.inputs)
        output_sig = type_to_sql_string(self.func.output)

        return '{}({}) returns {}'.format(scoped_name, input_sig, output_sig)


class CreateUDF(CreateFunction):
    def compile(self):
        create_decl = 'CREATE FUNCTION'
        impala_sig = self._impala_signature()
        param_line = "location '{}' symbol='{}'".format(
            self.func.lib_path, self.func.so_symbol
        )
        return ' '.join([create_decl, impala_sig, param_line])


class CreateUDA(CreateFunction):
    def compile(self):
        create_decl = 'CREATE AGGREGATE FUNCTION'
        impala_sig = self._impala_signature()
        tokens = ["location '{}'".format(self.func.lib_path)]

        fn_names = (
            'init_fn',
            'update_fn',
            'merge_fn',
            'serialize_fn',
            'finalize_fn',
        )

        for fn in fn_names:
            value = getattr(self.func, fn)
            if value is not None:
                tokens.append("{}='{}'".format(fn, value))

        return ' '.join([create_decl, impala_sig]) + ' ' + '\n'.join(tokens)


class DropFunction(DropObject):
    def __init__(
        self, name, inputs, must_exist=True, aggregate=False, database=None
    ):
        super().__init__(must_exist=must_exist)
        self.name = name
        self.inputs = tuple(map(dt.dtype, inputs))
        self.must_exist = must_exist
        self.aggregate = aggregate
        self.database = database

    def _impala_signature(self):
        full_name = self._get_scoped_name(self.name, self.database)
        input_sig = _impala_input_signature(self.inputs)
        return '{}({})'.format(full_name, input_sig)

    def _object_name(self):
        return self.name

    def compile(self):
        tokens = ['DROP']
        if self.aggregate:
            tokens.append('AGGREGATE')
        tokens.append('FUNCTION')
        if not self.must_exist:
            tokens.append('IF EXISTS')

        tokens.append(self._impala_signature())
        return ' '.join(tokens)


class ListFunction(BaseDDL):
    def __init__(self, database, like=None, aggregate=False):
        self.database = database
        self.like = like
        self.aggregate = aggregate

    def compile(self):
        statement = 'SHOW '
        if self.aggregate:
            statement += 'AGGREGATE '
        statement += 'FUNCTIONS IN {}'.format(self.database)
        if self.like:
            statement += " LIKE '{}'".format(self.like)
        return statement


def _impala_input_signature(inputs):
    # TODO: varargs '{}...'.format(val)
    return ', '.join(map(type_to_sql_string, inputs))
