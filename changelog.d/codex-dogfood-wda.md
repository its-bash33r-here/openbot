### Changed

- TrueSMAC reads a native iOS simulator screen in the main chat through Appium, without a manually started WebDriverAgent. The agent creates its own session on an approved, booted simulator and deletes only sessions it created.
