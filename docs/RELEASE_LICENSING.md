# Release licensing

## II PRO-authored code and documentation

II PRO Research releases its own code, tests, configuration and documentation under the [Apache License 2.0](../LICENSE). This grant covers material that II PRO Research has the right to license. Third-party notices and terms continue to apply to their respective components. [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)

## SIWC DevKit

The private research integration uses the OpenAI Sign in with ChatGPT DevKit pinned to `0a36fefeb913055c8c7a1a29b63d82b96b2e841a`. Its Noncommercial License v1.0 has independent terms for use and redistribution. Any later distribution that includes the DevKit needs a separate license review and must preserve the required notices and any applicable change notices. The Alpha omits the third-party source, compiled transport and upstream tests. [Pinned DevKit license](https://github.com/openai/sign-in-with-chatgpt-devkit/blob/0a36fefeb913055c8c7a1a29b63d82b96b2e841a/LICENSE)

The in-house adapter, bridge source, project contract and original `runtime-manifest.json` are retained. The runtime fails closed when inventoried files are missing. The local Studio can start and run its local UI and Core without that transport. Cloud generation via SIWC remains unavailable in this snapshot.

## Other dependencies

The production UI bundles React 19.3.1, React DOM 19.3.1 and Scheduler 0.28.0 under the MIT License. The exact notice text for each package is retained in [THIRD_PARTY_NOTICES.txt](../ui/THIRD_PARTY_NOTICES.txt). Those notices and the standard Apache-2.0 license text are the identified copied external texts. Docker images, model weights, installed development packages and the DevKit sources, compiled transport and tests are outside this snapshot. Dependencies listed in lockfiles are obtained separately under their upstream terms.

## Provenance and scope

The reviewed release inventory attributes project files to II PRO Research. References to Harness², Hermes Agent and Asset Vault describe research influences. The release inventory contains no copies of those projects. This inventory records the provenance checked for this shipment. It does not independently establish copyright ownership or grant rights to future contributions or dependencies.
