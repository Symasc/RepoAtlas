export async function fetchWidgets(): Promise<unknown> {
  const response = await fetch('https://widgets.example.test/v1/widgets')
  return response.json()
}

export async function fetchWidget(id: string): Promise<unknown> {
  const response = await fetch(`https://widgets.example.test/v1/widgets/${id}`)
  return response.json()
}

export async function fetchOrphans(): Promise<unknown> {
  const response = await fetch('https://widgets.example.test/orphans', { method: 'GET' })
  return response.json()
}
