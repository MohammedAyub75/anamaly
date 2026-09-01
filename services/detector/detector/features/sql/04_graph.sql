-- Feature block 4 -- graph-derived features.
--
-- Cluster sizes, manager depth, cycle and self-approval flags, all as set-based
-- SQL. Phase 5's graph layer walks the *candidate subgraphs* these columns
-- identify with networkx; nothing ever builds a million-node graph in memory,
-- and a cluster size of one -- the overwhelming majority -- never needs to.
--
-- Bank accounts are matched over history rather than over the current row: an
-- account shared for six months and then changed is still a shared account.

CREATE OR REPLACE TEMP TABLE graph_features AS
WITH RECURSIVE
iban_size AS (
    SELECT iban, count(DISTINCT employee_id) AS n
    FROM fact_bank_account
    WHERE iban IS NOT NULL
    GROUP BY iban
),
iban_per_employee AS (
    -- `is_known_benign_share` is deliberately not read here. It is eval-harness
    -- metadata (docs/DATA_DICTIONARY.md), and a detector that consumes it is
    -- being told the answer.
    SELECT b.employee_id,
           max(k.n)                               AS iban_cluster_size,
           count(DISTINCT b.iban)                 AS iban_count,
           max(CASE WHEN k.n > 1 THEN b.iban END) AS shared_iban
    FROM fact_bank_account b
    JOIN iban_size k USING (iban)
    GROUP BY b.employee_id
),
identity_size AS (
    SELECT national_id AS identifier, count(*) AS n
    FROM employee_master WHERE national_id IS NOT NULL GROUP BY 1
    UNION ALL
    SELECT iqama_no, count(*) FROM employee_master WHERE iqama_no IS NOT NULL GROUP BY 1
),
-- Two equality joins rather than one join on `national_id OR iqama_no`. The
-- OR form is the same answer and DuckDB cannot hash it: it degrades to a
-- nested loop, which is a second at 10k, a minute and a half at 100k and hours
-- at 1m. Every join in the feature build has to be an equality (phase 7).
identity_matches AS (
    SELECT e.employee_id, d.n
    FROM employee_master e
    JOIN identity_size d ON d.identifier = e.national_id
    UNION ALL
    SELECT e.employee_id, d.n
    FROM employee_master e
    JOIN identity_size d ON d.identifier = e.iqama_no
),
identity_per_employee AS (
    SELECT employee_id, max(n) AS identity_cluster_size
    FROM identity_matches
    GROUP BY employee_id
),
-- Walk up the manager chain. The depth cap is what makes a cycle terminate:
-- a chain that revisits its own root is a cycle, and stopping there is how the
-- flag is computed rather than a safety net bolted on afterwards.
walk(root, node, depth) AS (
    SELECT employee_id, manager_id, 1
    FROM employee_master
    WHERE manager_id IS NOT NULL
    UNION ALL
    SELECT w.root, e.manager_id, w.depth + 1
    FROM walk w
    JOIN employee_master e ON e.employee_id = w.node
    WHERE w.node IS NOT NULL AND w.node <> w.root AND w.depth < 30
),
chain AS (
    SELECT root AS employee_id,
           max(depth)          AS manager_depth,
           bool_or(node = root) AS manager_cycle_flag
    FROM walk
    GROUP BY root
),
self_approved AS (
    SELECT DISTINCT employee_id, TRUE AS approver_is_self_flag
    FROM fact_assignment_history
    WHERE approved_by = employee_id
),
approvals AS (
    SELECT approved_by AS employee_id, count(*) AS approvals_given
    FROM fact_assignment_history
    WHERE approved_by IS NOT NULL
    GROUP BY 1
)
SELECT e.employee_id,
       coalesce(ib.iban_cluster_size, 1)          AS iban_cluster_size,
       coalesce(ib.iban_count, 0)                 AS iban_count,
       ib.shared_iban,
       coalesce(id.identity_cluster_size, 1)      AS identity_cluster_size,
       coalesce(ch.manager_depth, 0)              AS manager_depth,
       coalesce(ch.manager_cycle_flag, FALSE)     AS manager_cycle_flag,
       coalesce(sa.approver_is_self_flag, FALSE)  AS approver_is_self_flag,
       coalesce(ap.approvals_given, 0)            AS approvals_given
FROM employee_master e
LEFT JOIN iban_per_employee ib USING (employee_id)
LEFT JOIN identity_per_employee id USING (employee_id)
LEFT JOIN chain ch USING (employee_id)
LEFT JOIN self_approved sa USING (employee_id)
LEFT JOIN approvals ap USING (employee_id);
