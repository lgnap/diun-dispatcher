# Virtual series: follow an image's patches when it publishes no series tag

Written on 2026-09-23 from the MigrateBoum session, to be picked up in this
repository. Follows PR #9 (only an `update` of the tag in service redeploys) and
PR #10 (announcements: `in-series`, `rolling`, `unmanaged`).

## The problem

The dispatcher always redeploys the tag **configured** in Coolify. An image
pinned on a series (`gitea/gitea:1.27`, `crazymax/diun:4`) therefore receives
its patches by itself: the publisher republishes the `1.27` tag, Diun sends
`update`, the dispatcher redeploys.

But some publishers **publish no series tag at all**: lychee (`v6.10.1`, no
`v6` nor `v6.10`), mealie (`v3.21.0`, no `v3`), n8n (`2.40.5`, no `2.40`).
Pinned on an exact version, they never receive anything: lychee published
`v6.10.2` to `v6.10.4` without boum seeing them. And the Diun label
`max_tags=1` (see "Diun configuration in place") only follows the highest tag
of the repository (a v7): a patch to an older major is not even announced.

## The principle: the dispatcher changes the tag itself, within a limit

A **per-resource policy**, declared by a label on the service, in the compose
Coolify holds:

| Label | Follows | Example, from `v6.10.1` |
|---|---|---|
| `diun-dispatcher.follow=patch` | same major and same minor | → `v6.10.4`, never `v6.11.0` |
| `diun-dispatcher.follow=minor` | same major | → `v6.11.0`, never `v7.0.0` |
| (none) | nothing | current behaviour: announce only |

For a resource carrying a policy, the dispatcher:

1. lists the repository's published tags (registry: Docker Hub, ghcr.io;
   private registries are out of scope at first);
2. keeps those that respect the policy relative to the configured tag, with the
   same form (`v` prefix, number of components, optional suffix); ignores
   pre-releases (`-rc`, `-beta`, `-legacy`…);
3. if the highest one is above the configured tag: rewrites **only the
   `image:` line** of the service in the Coolify compose (`PATCH
   /api/v1/services/{uuid}`, `docker_compose_raw` in base64), reads the compose
   back to check it changed only there, then
   `POST /services/{uuid}/restart?latest=true`, and follows the outcome as for
   an auto-deploy (`watch_deployment`);
4. notifies: "lychee v6.10.1 → v6.10.4 applied" (or the failure, with the
   original compose put back if the redeploy fails);
5. beyond the policy (a v7): the current announcement from PR #10.

## Trigger: a periodic check, not only Diun events

With `max_tags=1`, Diun emits nothing when a patch appears in an older major.
The most reliable option: an **internal periodic task** in the dispatcher (once
a day, configurable hour), which goes over the resources carrying
`diun-dispatcher.follow` only. A Diun `new` event for a relevant repository can
also trigger the check of that resource right away.

Safeguards:
- `AUTO_DEPLOY` also covers these upgrades: without it, a notification with a
  link, nothing applied;
- `IGNORE_CONTAINERS` applies;
- one upgrade at a time per resource, and at most one per day;
- never a major change, whatever the policy.

## Coolify permissions

The current token (`COOLIFY_TOKEN`) is limited to deployments (a call to
`/api/v1/servers` returns 403, noted in MigrateGrawie). Rewriting a compose
requires write access. **Decided 2026-09-24: widen the current token** rather
than add a second one, which would live in the same container and protect
nothing. Coolify permissions do not imply one another (`write` does not grant
`deploy`): `COOLIFY_TOKEN` needs `read`, `deploy`, `read:sensitive` and `write`.
The PATCH returns the
resource's variables **in clear**: never log its response.
Pitfalls already hit with this API (boum, 2026-09-23): Coolify adds quotes
around `image:` when saving (compare without quotes); a restart regenerates the
`.env` on the host.

## Diun configuration in place (boum, grawie, nounourse, 2026-09-23)

Per-container labels on pinned resources: `diun.watch_repo=true`,
`diun.include_tags=<image-specific filter>`, `diun.sort_tags=semver`,
`diun.max_tags=1`. Filters: lychee and mealie `^v\d+\.\d+\.\d+$`, n8n
`^\d+\.\d+\.\d+$`. Script: `MigrateBoum/scripts/diun-labels.py`. **Beware**:
`include_tags` also applies to the tag in service (a global filter made Diun
ignore every image outside the filter): a policy label must not change these
filters.

## First planned uses

- lychee (boum): `patch` on v6 (→ v6.10.4), then `minor` after moving to v7;
- mealie (grawie): `minor`;
- n8n (grawie): `patch` or `minor`, to be decided.

## Tests to write

Tag parsing (prefix, components, suffixes, pre-releases); target choice per
policy; rewriting the `image:` line (quotes, multiple services, read-back);
PATCH or redeploy failure → original compose restored; periodic task limited to
labelled resources; `AUTO_DEPLOY` and `IGNORE_CONTAINERS` respected.
