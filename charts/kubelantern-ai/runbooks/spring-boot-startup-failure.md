---
title: Spring Boot application failed to start
---
# Symptoms
The log shows a block starting with `APPLICATION FAILED TO START` or
`Error starting ApplicationContext`, for example:
- `Failed to configure a DataSource: 'url' attribute is not specified`
- `Could not resolve placeholder 'API_KEY' in value "${API_KEY}"`
- `Web server failed to start. Port 8080 was already in use.`
- `Parameter 0 of constructor in ... required a bean of type '...' that could not be found.`
- `org.flywaydb.core.api.FlywayException` / `Unable to obtain connection from database`

# Why it happens
Most Spring startup failures are configuration: a missing property or environment
variable, the wrong active profile, a database that is not reachable at startup,
or a bean that is only created in some profiles.

# What to check
1. Read the "Description" and "Action" lines of the failure block: Spring names the cause.
2. `SPRING_PROFILES_ACTIVE` and the environment variables in the Deployment.
3. Relaxed binding: property `spring.datasource.url` = env var `SPRING_DATASOURCE_URL`.
4. If the cause is the database, see the database runbooks.

# Fix
Provide the missing property (env var, ConfigMap, Secret), set the right profile,
or fix the dependency it could not reach. Avoid failing startup on optional
dependencies; use readiness probes (`/actuator/health/readiness`) instead.
