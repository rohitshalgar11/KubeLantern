---
title: .NET application fails at startup
---
# Symptoms
- `Unhandled exception. System.IO.FileNotFoundException: Could not load file or assembly 'X, Version=...'`
- `Unhandled exception. System.InvalidOperationException: Unable to resolve service for type '...'`
- `System.Net.Sockets.SocketException (13): Permission denied` / `Failed to bind to address http://[::]:80`
- `You must install or update .NET to run this application.` / `The framework 'Microsoft.AspNetCore.App', version '8.0.0' was not found.`
- `Microsoft.Data.SqlClient.SqlException: A network-related or instance-specific error occurred`

# Why it happens
A missing assembly or a runtime/framework version mismatch between build and base
image, a dependency-injection registration missing, configuration missing, a port
below 1024 as non-root (.NET 8 images run as non-root and listen on 8080), or a
database not reachable at startup.

# What to check
1. Base image version (`mcr.microsoft.com/dotnet/aspnet:8.0`) vs `TargetFramework` in the project.
2. `ASPNETCORE_URLS` / `ASPNETCORE_HTTP_PORTS` vs the container port and Service `targetPort`.
3. Environment: `ASPNETCORE_ENVIRONMENT`, connection strings (`ConnectionStrings__Default`).

# Fix
Use the matching runtime image, register the missing service, listen on 8080 and
map the Service to it, and provide missing configuration via env vars (`__` for
nested keys). Database errors: see the database runbooks.
