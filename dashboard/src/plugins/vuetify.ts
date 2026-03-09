import 'vuetify/styles'
import { createVuetify } from 'vuetify'
import { aliases, mdi } from 'vuetify/iconsets/mdi'

export default createVuetify({
  theme: {
    defaultTheme: 'dark',
    themes: {
      dark: {
        dark: true,
        colors: {
          // Void Space palette (Uncodixfy-approved)
          background: '#0d1117',
          surface: '#161b22',
          'surface-bright': '#1c2129',
          'surface-variant': '#21262d',
          'on-surface': '#c9d1d9',
          'on-background': '#c9d1d9',
          primary: '#58a6ff',
          'primary-darken-1': '#388bfd',
          secondary: '#79c0ff',
          'secondary-darken-1': '#58a6ff',
          success: '#3fb950',
          error: '#f85149',
          warning: '#d29922',
          info: '#58a6ff',
          accent: '#f78166',
        },
        variables: {
          'border-color': '#ffffff',
          'border-opacity': 0.06,
          'high-emphasis-opacity': 0.95,
          'medium-emphasis-opacity': 0.55,
          'disabled-opacity': 0.3,
          'hover-opacity': 0.06,
          'focus-opacity': 0.1,
          'activated-opacity': 0.1,
          'pressed-opacity': 0.14,
        },
      },
    },
  },
  defaults: {
    // Uncodixfy: no oversized rounded corners, no pill shapes
    VCard: {
      rounded: 'md',
      variant: 'flat',
      color: 'surface',
    },
    VBtn: {
      rounded: 'md',
      variant: 'flat',
    },
    VChip: {
      rounded: 'md',
    },
    VTextField: {
      variant: 'solo-filled',
      density: 'compact',
      rounded: 'md',
      flat: true,
      bgColor: 'surface-bright',
      hideDetails: true,
    },
    VTextarea: {
      variant: 'solo-filled',
      density: 'compact',
      rounded: 'md',
      flat: true,
      bgColor: 'surface-bright',
      hideDetails: true,
    },
    VSelect: {
      variant: 'solo-filled',
      density: 'compact',
      rounded: 'md',
      flat: true,
      bgColor: 'surface-bright',
      hideDetails: true,
    },
    VTable: {
      density: 'comfortable',
    },
  },
  icons: {
    defaultSet: 'mdi',
    aliases,
    sets: { mdi },
  },
})
