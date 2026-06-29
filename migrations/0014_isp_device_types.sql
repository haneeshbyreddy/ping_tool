-- 0014_isp_device_types.sql — normalize legacy telecom device categories to the ISP set.
--
-- The node "type" pick-list moved from wireless-telecom terms (tower / relay / sector) to
-- an ISP infrastructure taxonomy (core / router / switch / gateway / OLT / AP / CPE /
-- backhaul). `device_type` is a DISPLAY-ONLY label — it gates only the edit-form
-- validation, nothing functional (monitoring, topology, alerting all ignore it) — so this
-- remap is purely cosmetic and safe. Doing it here keeps existing inventory valid in the
-- new pick-list instead of silently blanking a node's type the next time it's edited.
--
-- Mapping: the wireless-access sites (tower, sector radio) -> AP (access point); a relay
-- hop -> backhaul (transport). core / backhaul already exist in the new set and are left
-- as-is. Idempotent: the runner applies each file once, and the UPDATEs only ever touch
-- the three legacy values, so a re-run (or a fresh DB with no legacy rows) is a no-op.
-- Re-tag any node from the Nodes page if a different ISP category fits it better.

UPDATE devices SET device_type = 'AP'       WHERE device_type IN ('tower', 'sector');
UPDATE devices SET device_type = 'backhaul' WHERE device_type = 'relay';
