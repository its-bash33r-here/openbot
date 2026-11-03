### Changed

- TrueSMAC reads a native iOS simulator screen in the main chat through Appium, without a manually started WebDriverAgent. The agent creates its own session on an approved, booted simulator and deletes only sessions it created.

### Fixed

- The registered Appium MCP server is no longer given to an agent whose Computer Use is off. Before, the agent could still reach a simulator or device through Appium, because only Computer Use was removed from its tools.
