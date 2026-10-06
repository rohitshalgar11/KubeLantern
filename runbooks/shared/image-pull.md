---
title: ImagePullBackOff / ErrImagePull
category: image
---
# Symptoms
Pod stuck in ErrImagePull or ImagePullBackOff; the container never starts, so there are no logs.

# What to check
1. Events: "not found" or "manifest unknown" means the image name or tag is wrong.
2. "unauthorized", "pull access denied" or "authentication required" means registry credentials are missing.
3. Does the pod (or its ServiceAccount) have the right imagePullSecrets for a private registry?
4. Was the tag deleted or never pushed by the CI pipeline?

# Fix
Correct the image reference in the Deployment, push the missing tag, or add an
imagePullSecret for the registry to the pod or its ServiceAccount.
