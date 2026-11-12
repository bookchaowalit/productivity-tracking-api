import { defineRailway, project, service } from "railway/iac";

// productivity-tracking-api: the Tracking cell (SPEC-003). Variables stay unmanaged (secrets live in Railway only).
// tracking-api sleeps when idle (Railway Serverless) and wakes on the next request.
// Region: Singapore next to Neon (aws-ap-southeast-1); set once after the first deploy:
//   railway scale --service tracking-api asia-southeast1-eqsg3a=1 us-east4-eqdc4a=0
// Deploys are `railway up` of a CI-green commit (CLI), like the other cells whose GitHub source cannot be connected.
export default defineRailway(() => {
  const api = service("tracking-api", {
    healthcheck: "/health",
    healthcheckTimeout: 60,
    deploy: { sleepApplication: true },
  });

  return project("productivity-tracking-api", {
    variables: { managed: false },
    resources: [api],
  });
});
