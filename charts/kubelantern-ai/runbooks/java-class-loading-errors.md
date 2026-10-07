---
title: Java ClassNotFoundException, NoClassDefFoundError or UnsupportedClassVersionError
category: application-error
---
# Symptoms
- `java.lang.ClassNotFoundException: org.postgresql.Driver`
- `java.lang.NoClassDefFoundError: com/fasterxml/jackson/databind/ObjectMapper`
- `java.lang.UnsupportedClassVersionError: ... compiled by a more recent version of the Java Runtime (class file version 65.0), this version of the Java Runtime only recognizes class file versions up to 61.0`
- `java.lang.NoSuchMethodError` after a dependency upgrade

# Why it happens
A dependency is missing from the built JAR/classpath, two versions of a library
conflict, or the code was compiled for a newer Java than the image's runtime
(class file 61 = Java 17, 65 = Java 21).

# What to check
1. Does the base image's Java version match the build's target (`java -version` in the image)?
2. Was the JAR built as a fat/uber JAR, or is the classpath set correctly?
3. Recent dependency upgrades: `mvn dependency:tree` / `gradle dependencies` for conflicts.

# Fix
Use a runtime image with the same Java version the code was compiled for, add the
missing dependency (scope `runtime`, not `provided` or `test`), and resolve version
conflicts. Roll back if this started with a release.
