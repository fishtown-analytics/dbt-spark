from typing import List, Dict

import agate
import dbt.exceptions
from dbt.adapters.base import available
from dbt.adapters.sql import SQLAdapter
from dbt.contracts.graph.manifest import Manifest
from dbt.logger import GLOBAL_LOGGER as logger

from dbt.adapters.spark import SparkConnectionManager
from dbt.adapters.spark import SparkRelation

LIST_RELATIONS_MACRO_NAME = 'list_relations_without_caching'
GET_RELATION_TYPE_MACRO_NAME = 'spark_get_relation_type'
DROP_RELATION_MACRO_NAME = 'drop_relation'
FETCH_TBLPROPERTIES_MACRO_NAME = 'spark_fetch_tblproperties'

KEY_TABLE_OWNER = 'Owner'


class SparkAdapter(SQLAdapter):
    ConnectionManager = SparkConnectionManager
    Relation = SparkRelation

    AdapterSpecificConfigs = frozenset({"file_format", "location_root",
                                        "partition_by", "clustered_by",
                                        "buckets"})

    column_names = (
        'table_database',
        'table_schema',
        'table_name',
        'table_type',
        'table_comment',
        'table_owner',
        'column_name',
        'column_index',
        'column_type',
        'column_comment'
    )

    @classmethod
    def date_function(cls):
        return 'CURRENT_TIMESTAMP()'

    @classmethod
    def convert_text_type(cls, agate_table, col_idx):
        return "STRING"

    @classmethod
    def convert_number_type(cls, agate_table, col_idx):
        decimals = agate_table.aggregate(agate.MaxPrecision(col_idx))
        return "DOUBLE" if decimals else "BIGINT"

    @classmethod
    def convert_datetime_type(cls, agate_table, col_idx):
        return "TIMESTAMP"

    def get_relation_type(self, relation, model_name=None):
        kwargs = {'relation': relation}
        return self.execute_macro(
            GET_RELATION_TYPE_MACRO_NAME,
            kwargs=kwargs,
            release=True
        )

    # Override that creates macros without a known type - adapter macros that
    # require a type will dynamically check at query-time
    def list_relations_without_caching(self, information_schema, schema,
                                       model_name=None) -> List:
        kwargs = {'information_schema': information_schema, 'schema': schema}
        try:
            results = self.execute_macro(
                LIST_RELATIONS_MACRO_NAME,
                kwargs=kwargs,
                release=True
            )
        except dbt.exceptions.RuntimeException as e:
            if hasattr(e, 'msg') and f"Database '{schema}' not found" in e.msg:
                return []

        relations = []
        quote_policy = {
            'schema': True,
            'identifier': True
        }
        for _database, name, _, information in results:
            rel_type = ('view' if 'Type: VIEW' in information else 'table')
            relations.append(self.Relation.create(
                database=_database,
                schema=_database,
                identifier=name,
                quote_policy=quote_policy,
                type=rel_type
            ))
        return relations

    def get_relation(self, database, schema, identifier):
        relations_list = self.list_relations(schema, schema)

        matches = self._make_match(relations_list=relations_list,
                                   database=None, schema=schema,
                                   identifier=identifier)

        if len(matches) > 1:
            kwargs = {
                'identifier': identifier,
                'schema': schema
            }
            dbt.exceptions.get_relation_returned_multiple_results(
                kwargs, matches
            )

        elif matches:
            return matches[0]

        return None

    # Override that doesn't check the type of the relation -- we do it
    # dynamically in the macro code
    def drop_relation(self, relation, model_name=None):
        if dbt.flags.USE_CACHE:
            self.cache.drop(relation)

        self.execute_macro(
            DROP_RELATION_MACRO_NAME,
            kwargs={'relation': relation}
        )

    @staticmethod
    def find_table_information_separator(rows: List[dict]) -> int:
        pos = 0
        for row in rows:
            if not row['col_name']:
                break
            pos += 1
        return pos

    @available
    def parse_describe_extended(self, relation: Relation, raw_rows: List[agate.Row]):
        # Convert the Row to a dict
        dict_rows = [dict(zip(row._keys, row._values)) for row in raw_rows]
        # Find the separator between the rows and the metadata provided by extended
        pos = SparkAdapter.find_table_information_separator(dict_rows)

        # Remove rows that start with a hash, they are comments
        rows = [row for row in raw_rows[0:pos] if not row['col_name'].startswith('#')]
        metadata = {col['col_name']: col['data_type'] for col in raw_rows[pos + 1:]}

        return [dict(zip(self.column_names, (
            relation.database,
            relation.schema,
            relation.name,
            relation.type,
            None,
            metadata.get(KEY_TABLE_OWNER),
            column['col_name'],
            idx,
            column['data_type'],
            None
        ))) for idx, column in enumerate(rows)]

    def get_properties(self, relation: Relation) -> Dict[str, str]:
        properties = self.execute_macro(
            FETCH_TBLPROPERTIES_MACRO_NAME,
            kwargs={'relation': relation}
        )
        return {key: value for (key, value) in properties}

    def get_catalog(self, manifest: Manifest):
        schemas = manifest.get_used_schemas()

        columns = []
        for (database_name, schema_name) in schemas:
            relations = self.list_relations(database_name, schema_name)
            for relation in relations:
                logger.debug("Getting table schema for relation {}", relation)
                columns += self.get_columns_in_relation(relation)

        return dbt.clients.agate_helper.table_from_data(
            columns, self.column_names)
