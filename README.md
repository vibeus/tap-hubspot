# tap-hubspot

This is a [Singer](https://singer.io) tap that produces JSON-formatted data following the [Singer spec](https://github.com/singer-io/getting-started/blob/master/SPEC.md).

This tap:
- Pulls raw data from HubSpot's [REST API](http://developers.hubspot.com/docs/overview)
- Extracts the following resources from HubSpot
  - [Campaigns](http://developers.hubspot.com/docs/methods/email/get_campaign_data)
  - [Companies](http://developers.hubspot.com/docs/methods/companies/get_company)
  - [Contacts](https://docs.stitchdata.com/hc/en-us/articles/URL)
  - [Contact Lists](http://developers.hubspot.com/docs/methods/lists/get_lists)
  - [Deals](http://developers.hubspot.com/docs/methods/deals/get_deals_modified)
  - [Email Events](http://developers.hubspot.com/docs/methods/email/get_events)
  - [Engagements](https://developers.hubspot.com/docs/methods/engagements/get-all-engagements)
  - [Forms](http://developers.hubspot.com/docs/methods/forms/v2/get_forms)
  - [Keywords](http://developers.hubspot.com/docs/methods/keywords/get_keywords)
  - [Owners](http://developers.hubspot.com/docs/methods/owners/get_owners)
  - [Subscription Changes](http://developers.hubspot.com/docs/methods/email/get_subscriptions_timeline)
  - [Workflows](http://developers.hubspot.com/docs/methods/workflows/v3/get_workflows)
- Outputs the schema for each resource
- Incrementally pulls data based on the input state

## Configuration

This tap requires a `config.json` which specifies details regarding [OAuth 2.0](https://developers.hubspot.com/docs/methods/oauth2/oauth2-overview) authentication, a cutoff date for syncing historical data, and an optional flag which controls collection of anonymous usage metrics. See [config.sample.json](config.sample.json) for an example.

To run `tap-hubspot` with the configuration file, use this command:

```bash
› tap-hubspot -c my-config.json
```


## API Key Authentication (for development)

As an alternative to OAuth 2.0 authentication during development, you may specify an API key (`HAPIKEY`) to authenticate with the HubSpot API. This should be used only for low-volume development work, as the [HubSpot API Usage Guidelines](https://developers.hubspot.com/apps/api_guidelines) specify that integrations should use OAuth for authentication.

To use an API key, set the `HUBSPOT_HAPIKEY` environment variable with the value of your HAPIKEY when running `tap-hubspot`. Any authentication parameters in your `config.json` will be ignored.

---

Copyright &copy; 2017 Stitch
