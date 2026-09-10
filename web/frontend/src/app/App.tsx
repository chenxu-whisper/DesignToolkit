import { AppShell } from './AppShell'
import { ThemeProvider } from './theme/ThemeProvider'

export function App() {
  return (
    <ThemeProvider>
      <AppShell />
    </ThemeProvider>
  )
}
