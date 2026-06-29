-- 0013_port_bandwidth.sql — per-port throughput (bandwidth) stats + low-bandwidth alarm.
--
-- Phase 9 Part B (0012) gave us oper/admin port *status*; this adds the orthogonal
-- *traffic* view. Each SNMP walk reads the 64-bit IF-MIB byte counters
-- (ifHCInOctets/ifHCOutOctets); the daemon diffs them against the previous walk to get a
-- throughput RATE (bits/sec) and alarms when a monitored port's rate falls BELOW an
-- operator-assigned threshold — an uplink that went quiet (a silent failover, a partial
-- fault, a customer link that stopped delivering) rather than one that hard-dropped.
--
-- Same disciplines as the rest of switch_ports:
--   * operator-set fields (bw_threshold_mbps, bw_direction) are NEVER touched by a walk,
--     exactly like monitored / feeds_device_id;
--   * the flap-suppressed alarm state (bw_low_streak / bw_alarm / bw_alarm_since) lives
--     in-row so a daemon restart never loses the streak or re-pages a still-low port.
-- Octet counters are TEXT: a Counter64 can exceed SQLite's signed-64 INTEGER range, and
-- the rate math is done in Python with arbitrary-precision ints. All columns are nullable
-- or defaulted, so existing rows upgrade cleanly. The runner applies each file once, so
-- the bare ALTERs are safe (same pattern as 0012/0009/0007).

ALTER TABLE switch_ports ADD COLUMN bw_threshold_mbps REAL;    -- operator: low-bw alarm floor (Mbps); NULL = no bw alarm
ALTER TABLE switch_ports ADD COLUMN bw_direction      TEXT;    -- operator: 'in'|'out'|'either'|'total' (NULL => 'either')
ALTER TABLE switch_ports ADD COLUMN in_octets         TEXT;    -- last ifHCInOctets (raw Counter64, as text)
ALTER TABLE switch_ports ADD COLUMN out_octets        TEXT;    -- last ifHCOutOctets (raw Counter64, as text)
ALTER TABLE switch_ports ADD COLUMN counters_at       TEXT;    -- ts of the last counter reading (for the rate delta)
ALTER TABLE switch_ports ADD COLUMN in_bps            REAL;    -- last computed inbound rate (bits/sec)
ALTER TABLE switch_ports ADD COLUMN out_bps           REAL;    -- last computed outbound rate (bits/sec)
ALTER TABLE switch_ports ADD COLUMN if_speed_bps      REAL;    -- link negotiated speed (ifHighSpeed/ifSpeed), bits/sec
ALTER TABLE switch_ports ADD COLUMN bw_low_streak     INTEGER NOT NULL DEFAULT 0;  -- consecutive below-threshold walks
ALTER TABLE switch_ports ADD COLUMN bw_alarm          INTEGER NOT NULL DEFAULT 0;  -- confirmed low-bandwidth (post flap-suppression)
ALTER TABLE switch_ports ADD COLUMN bw_alarm_since    TEXT;
