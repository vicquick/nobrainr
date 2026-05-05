import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: '/galaxy',
    },
    {
      path: '/galaxy',
      name: 'galaxy',
      component: () => import('@/views/GalaxyView.vue'),
    },
    {
      path: '/graph',
      name: 'graph',
      component: () => import('@/views/GraphView.vue'),
    },
    {
      path: '/memories',
      name: 'memories',
      component: () => import('@/views/MemoriesView.vue'),
    },
    {
      path: '/timeline',
      name: 'timeline',
      component: () => import('@/views/TimelineView.vue'),
    },
    {
      path: '/scheduler',
      name: 'scheduler',
      component: () => import('@/views/SchedulerView.vue'),
    },
    {
      path: '/pulse',
      name: 'pulse',
      component: () => import('@/views/PulseView.vue'),
    },
    {
      path: '/commonplace',
      name: 'commonplace',
      component: () => import('@/views/CommonplaceView.vue'),
    },
    {
      path: '/insights',
      name: 'insights',
      component: () => import('@/views/InsightsView.vue'),
    },
    {
      path: '/threads',
      name: 'threads',
      component: () => import('@/views/ThreadsView.vue'),
    },
    {
      path: '/threads/:id',
      name: 'thread-detail',
      component: () => import('@/views/ThreadDetailView.vue'),
    },
  ],
})

export default router
