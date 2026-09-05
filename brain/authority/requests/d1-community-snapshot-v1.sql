WITH snapshot_rows(sort_group, record_type, record_key, payload) AS (
  SELECT
    1,
    'article',
    slug,
    json_object(
      'slug', slug,
      'wikipedia_title', wikipedia_title,
      'display_title', display_title,
      'wikidata_qid', wikidata_qid,
      'revid', revid,
      'latest_revid', latest_revid,
      'last_upstream_check', last_upstream_check,
      'annotations', annotations,
      'schema_version', schema_version,
      'version', version,
      'n_formalized', n_formalized,
      'n_partial', n_partial,
      'n_not_formalized', n_not_formalized,
      'created_at', created_at,
      'updated_at', updated_at
    )
  FROM articles

  UNION ALL

  SELECT
    2,
    'brain_edge',
    id,
    json_object(
      'id', id,
      'src', src,
      'dst', dst,
      'kind', kind,
      'evidence', evidence,
      'added_by', added_by,
      'actor_type', actor_type,
      'status', status,
      'created_at', created_at,
      'deleted_by', deleted_by,
      'deleted_at', deleted_at,
      'version', version
    )
  FROM brain_edges

  UNION ALL

  SELECT
    3,
    'brain_node',
    id,
    json_object(
      'id', id,
      'label', label,
      'description', description,
      'node_type', node_type,
      'added_by', added_by,
      'actor_type', actor_type,
      'status', status,
      'created_at', created_at,
      'deleted_by', deleted_by,
      'deleted_at', deleted_at,
      'version', version
    )
  FROM brain_nodes
), control_row(sort_group, record_type, record_key, payload) AS (
  SELECT
    4,
    'control',
    'counts',
    json_object(
      'schema', 'wikilean.d1-snapshot-control/v1',
      'articles', (SELECT count(*) FROM articles),
      'brain_edges', (SELECT count(*) FROM brain_edges),
      'brain_nodes', (SELECT count(*) FROM brain_nodes),
      'article_columns', (
        SELECT json_group_array(name)
        FROM (SELECT name FROM pragma_table_info('articles') ORDER BY cid)
      ),
      'brain_edge_columns', (
        SELECT json_group_array(name)
        FROM (SELECT name FROM pragma_table_info('brain_edges') ORDER BY cid)
      ),
      'brain_node_columns', (
        SELECT json_group_array(name)
        FROM (SELECT name FROM pragma_table_info('brain_nodes') ORDER BY cid)
      ),
      'rows_total', (
        (SELECT count(*) FROM articles) +
        (SELECT count(*) FROM brain_edges) +
        (SELECT count(*) FROM brain_nodes)
      )
    )
)
SELECT record_type, record_key, payload
FROM (
  SELECT sort_group, record_type, record_key, payload FROM snapshot_rows
  UNION ALL
  SELECT sort_group, record_type, record_key, payload FROM control_row
)
ORDER BY sort_group, record_key
