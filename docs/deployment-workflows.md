# Deployment workflows

Site content is rendered in a read-only job with Hugo Extended 0.119.0. Deployment workflows load the build helper and checksum-verified package from the exact workflow commit, and render the exact selected content commit in a separate checkout. The package is extracted without running installation scripts. Hugo updates require a reviewed change to the trusted version, package, and digest before deployments can use them.

Pull request CI tests the proposed helper without deployment credentials, using the base commit's package. A package upgrade therefore needs its trusted tooling change merged before CI or deployments render with the new package.

`.noop` renders the site without publishing it. The result job passes every required job result and the original trigger context to Branch Deploy result mode. It reports failed, skipped, and cancelled work accurately; sticky locks remain until the normal unlock path. The site's `version.txt` records the selected content SHA.

Rerun the complete workflow to repeat a deployment. A partial rerun cannot reuse an earlier trigger attempt. If cancellation or runner loss prevents finalization, inspect the deployment record and its current lock before using the normal authorized recovery commands. Do not remove a replacement lock or repeat a successful publication solely because reporting failed.

Run the secretless helper checks with `python3 script/ci/test_build_site.py`. Live IssueOps verification requires the workflow changes on the default branch.
