declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}

interface Window {
  pywebview?: {
    api?: {
      select_project: () => Promise<string | null>
      save_bundle: (taskId: string) => Promise<string | null>
      open_output: (taskId: string) => Promise<boolean>
    }
  }
}
