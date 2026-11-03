// The application updater, its preferences, and the restart an admin of a joined server asked for.

import { runCauseEffect } from "../../backend/effect-boundary";
import type { ForkUpdate } from "../fork-update";
import type { IdleRestart } from "../idle-restart";
import type { RequestedUpdate } from "../requested-update";
import { readUpdatePreference } from "../update-preference-store";
import type { UpdateService } from "../update-service";
import { parseIdleRestartTarget, parseUpdatePreference } from "./app-inputs";
import { eventHandler, handler, type IpcGroupHandlers, payloadHandler } from "./define-ipc-group";

export interface UpdateIpcDependencies {
  updater: UpdateService;
  forkUpdate: ForkUpdate;
  updatePreferenceFile: string;
  requestedUpdate: Pick<RequestedUpdate, "cancel" | "setPreference">;
  idleRestart: Pick<IdleRestart, "request" | "cancel">;
}

export function updateIpcHandlers({
  updater,
  forkUpdate,
  updatePreferenceFile,
  requestedUpdate,
  idleRestart,
}: UpdateIpcDependencies): Pick<IpcGroupHandlers, "update"> {
  return {
    update: {
      getForkPhase: eventHandler((event) => runCauseEffect(forkUpdate.phase(event))),
      startFork: eventHandler((event) => runCauseEffect(forkUpdate.start(event))),
      getStatus: handler(() => updater.getStatus()),
      check: handler(() => runCauseEffect(updater.checkForUpdates())),
      download: handler(() => runCauseEffect(updater.downloadUpdate())),
      install: handler(() => runCauseEffect(updater.installUpdate())),
      getPreference: handler(() => runCauseEffect(readUpdatePreference(updatePreferenceFile))),
      setPreference: payloadHandler(parseUpdatePreference, (parsed) =>
        runCauseEffect(requestedUpdate.setPreference(parsed)),
      ),
      cancelScheduledRestart: handler(() => {
        requestedUpdate.cancel();
        return updater.getStatus();
      }),
      restartWhenIdle: payloadHandler(parseIdleRestartTarget, (target) => idleRestart.request(target)),
      cancelIdleRestart: handler(() => idleRestart.cancel()),
    },
  };
}
