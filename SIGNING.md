# Code signing

Release builds are made by GitHub Actions (`.github/workflows/build.yml`) from a tag, never on a
personal machine, so the signed exe is exactly what the public source produces. The PyInstaller
bootloader is compiled from source during the build (the prebuilt one is a frequent antivirus
false-positive trigger).

Signing uses the free open-source programme of [SignPath Foundation](https://signpath.org).
The workflow already contains the signing step; it stays switched off until the steps below are done.

## One-time setup (repository owner)

1. Apply at <https://signpath.org/apply> with this repository. They check that the project is open
   source, has a code signing policy (below) and builds its releases on CI.
2. After approval, in the SignPath web app:
   - create the project with slug `clip2vtf`, linked to this GitHub repository;
   - add an artifact configuration with slug `exe`:
     ```xml
     <?xml version="1.0" encoding="utf-8"?>
     <artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
       <zip-file>
         <pe-file path="clip2vtf.exe">
           <authenticode-sign />
         </pe-file>
       </zip-file>
     </artifact-configuration>
     ```
   - use the signing policy `release-signing` (SignPath Foundation certificate);
   - create an API token for a CI user that may submit signing requests.
3. In the GitHub repository settings → Secrets and variables → Actions:
   - variable `SIGNPATH_ORGANIZATION_ID` = the organization id from SignPath;
   - secret `SIGNPATH_API_TOKEN` = the API token.
4. Release: `git tag v1.1.0 && git push origin v1.1.0`. The workflow builds, submits the exe for
   signing, waits for approval and publishes a release with the signed exe.

## Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by
[SignPath Foundation](https://signpath.org).

- Committers and reviewers: [fedya2014fb-ru](https://github.com/fedya2014fb-ru)
- Approvers: [fedya2014fb-ru](https://github.com/fedya2014fb-ru)

Only binaries built by this repository's GitHub Actions workflow from a release tag are signed.

## Privacy

This program does not send any information anywhere unless you ask it to: it downloads an image
only when you paste or drop a link to it, and it talks to Hammer++ only on your own computer.
It has no telemetry and no update check.
