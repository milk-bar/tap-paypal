This is a [Singer](https://singer.io) tap that produces JSON-formatted data
following the [Singer
spec](https://github.com/singer-io/getting-started/blob/master/SPEC.md).

This tap:

- Pulls raw data from the [PayPal Sync API](https://developer.paypal.com/docs/api/sync/v1/)
- Extracts the following resources:
  - [transaction_info](https://developer.paypal.com/docs/api/sync/v1/#definition-transaction_info)
  - [payer_info](https://developer.paypal.com/docs/api/sync/v1/#definition-payer_info)
  - [shipping_info](https://developer.paypal.com/docs/api/sync/v1/#definition-shipping_info)
  - [auction_info](https://developer.paypal.com/docs/api/sync/v1/#definition-auction_info)
  - [cart_info](https://developer.paypal.com/docs/api/sync/v1/#definition-cart_info)
  - [incentive_info](https://developer.paypal.com/docs/api/sync/v1/#definition-incentive_info)
  - [store_info](https://developer.paypal.com/docs/api/sync/v1/#definition-store_info)

- Outputs the schema for each resource
- Incrementally pulls data based on the `transaction_updated_date` of each transaction

---
