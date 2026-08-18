# Hand-authored feature — not compiled from adcp-req.
#
# LOCALLY-ADDED (survives BR-*.feature regeneration).
# Upstream gap: the adcp-req storyboards have no scenario for seller-side TMP
# Package Sync — AdCP 3.1.1 trusted-match specification, "Package Sync": package
# metadata is synced from seller agents to TMP providers at media buy creation
# time and whenever the media buy materially changes. The obligation is
# transport-blind buyer-triggered behavior, so it belongs to a scenario rather
# than to a per-tier test that invents its own observable (#1197 review).
# Reconcile upstream in adcp-req, then retire this file for the regenerated
# scenario.
#
# Ungraded upstream: no conformance storyboard step covers Package Sync (the
# task is experimental in 3.1.1), so these scenarios are the local grading.
Feature: TMP package sync — registered providers receive package data (local)

  Background:
    Given a TMP provider is registered for the tenant

  @T-TMP-SYNC-create @trusted_match @experimental
  Scenario: creating a media buy delivers its packages to the provider
    When the Buyer Agent creates a media buy
    Then the provider receives the packages for that media buy

  @T-TMP-SYNC-update @trusted_match @experimental
  Scenario: updating a media buy re-delivers its packages to the provider
    Given the Buyer Agent created a media buy whose packages were delivered
    When the Buyer Agent updates that media buy
    Then the provider receives the packages for that media buy a second time
