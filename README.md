# Hush hosted agent tutorial

A hosted agent works for many users: it runs on your platform, not on a
user's machine. This tutorial shows Hush putting such an agent under control,
in your own cluster, in a few guided steps:

- **Identity without secrets**: the agent proves who it is with its Kubernetes
  token. Hush knows it by that identity, and nothing to steal is stored with it.
- **Token brokering**: Hush trades the agent's token and the signed-in user's
  for one access token that names both. The gateway holds each user's
  credentials to the app; the agent never sees them.
- **Access control**: the agent reaches only the apps assigned to it, and only
  for the users allowed to use it.
- **Guardrails**: every call is checked against the tool's policy. A read goes
  through, a write waits for the user's approval, a delete is blocked.

The agent reaches apps only through your Hush Identity Gateway. The chart
deploys an expenses assistant (the agent) and Expenses, a sample app it works
on, in their own namespace.

## Before you start

- A Hush Identity Gateway, and admin access to the Hush UI
- A Kubernetes cluster whose service account token issuer is reachable from
  the internet, as on EKS, GKE or AKS. Hush checks the agent's token there.
- Helm 3 and kubectl
- A Hush API key with only the Applications Read and Applications Write
  scopes, expiring in a few hours. The tutorial uses it to put Expenses in
  Hush, and to take it out when you uninstall.

## Run it

Put the API key in a Secret:

```bash
kubectl create namespace hush-hosted-agent-tutorial
kubectl create secret generic hush-api-key -n hush-hosted-agent-tutorial \
  --from-literal=id=<key id> --from-literal=secret=<key secret>
```

Any hosted agent working through Hush needs two values from its gateway: its
address (`gateway.url`), where the agent reaches your apps, and its issuer
(`gateway.issuer`), the audience its platform token must be issued for, so Hush
accepts that token as the agent's identity.

Read your gateway's issuer:

```bash
curl -s https://<gateway>/.well-known/oauth-protected-resource/mcp
```

and take the first of `authorization_servers`. Then install:

```bash
helm install tutorial oci://ghcr.io/hushsecurity/hush-hosted-agent-tutorial \
  -n hush-hosted-agent-tutorial \
  --set gateway.url=https://<gateway>/mcp \
  --set gateway.issuer=<issuer> \
  --set hush.apiKey.existingSecret=hush-api-key
kubectl port-forward -n hush-hosted-agent-tutorial svc/hush-hosted-agent-tutorial-agent 8080:8080
```

Open <http://localhost:8080> and follow the page. If the page loses the
agent, run the port-forward again.

## What you'll see

1. **Register the agent** in Hush, by its Kubernetes identity.
2. **Grant it access to Expenses**, and see the policy Hush set on each tool.
3. **Try the assistant**: sign in, connect your Expenses account, then list,
   add and delete expenses, and see what Hush lets through.
4. **Recap**, and how to clean up.

## What it installs

- In `hush-hosted-agent-tutorial`: the agent and its page, Expenses, a setup
  service that holds the API key, and a Cloudflare quick tunnel.
- In Hush: "Expenses (tutorial)", in the catalog and on your gateway for all
  users.

## Expenses is public

The gateway must reach Expenses, so by default a Cloudflare quick tunnel gives
it a random public address. Cloudflare sees that traffic, and signing in to
Expenses asks for an access code the page shows. Some endpoint security tools
flag `cloudflared`.

To use your own address instead, put a load balancer or ingress with HTTPS in
front of the `hush-hosted-agent-tutorial-expenses` Service (port 8000), and set
`expenses.url` to its address at install.

If the tunnel's pod restarts, its address changes. Restart Expenses and setup
so Hush learns the new one:

```bash
kubectl rollout restart -n hush-hosted-agent-tutorial \
  deploy/hush-hosted-agent-tutorial-expenses deploy/hush-hosted-agent-tutorial-setup
```

## Clean up

Uninstall before the API key expires: it takes Expenses out of Hush too.

```bash
helm uninstall tutorial -n hush-hosted-agent-tutorial
kubectl delete namespace hush-hosted-agent-tutorial
```

Then, in the Hush UI, delete the agent, and the API key unless it has expired.

If uninstalling fails, for example because the key expired, remove "Expenses
(tutorial)" in the Hush UI, first from Applications, then from Catalog, and
run the uninstall again with `--no-hooks`.
