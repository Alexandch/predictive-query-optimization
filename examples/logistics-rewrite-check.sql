SELECT s.shipment_id, s.tracking_code, s.shipment_status
FROM logistics.shipments AS s
WHERE s.shipment_id BETWEEN 1000 AND 1049
  AND (
      SELECT COUNT(*)
      FROM logistics.tracking_events AS te
      WHERE te.shipment_id = s.shipment_id
  ) > 0
ORDER BY s.shipment_id;
