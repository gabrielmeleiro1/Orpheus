export async function restartAndReconnectSetup(
  restart: () => Promise<void>,
  reconnect: () => void,
): Promise<void> {
  await restart();
  reconnect();
}

export async function cancelActiveSetup<T>(cancel: () => Promise<T>): Promise<T> {
  return cancel();
}
