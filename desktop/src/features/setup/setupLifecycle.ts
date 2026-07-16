export async function restartAndReconnectSetup(
  restart: () => Promise<void>,
  reconnect: () => void,
): Promise<void> {
  await restart();
  reconnect();
}
