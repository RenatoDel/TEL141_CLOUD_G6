import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from '@/components/ui/sonner'
import Layout from '@/components/Layout'
import Dashboard from '@/pages/Dashboard'
import Slices from '@/pages/Slices'
import SliceNew from '@/pages/SliceNew'
import JobStatus from '@/pages/JobStatus'

const queryClient = new QueryClient()

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Layout>
          <Routes>
            <Route path="/"           element={<Dashboard />} />
            <Route path="/slices"     element={<Slices />} />
            <Route path="/slices/new" element={<SliceNew />} />
            <Route path="/jobs/:id" element={<JobStatus />} />
          </Routes>
        </Layout>
      </BrowserRouter>
      <Toaster />
    </QueryClientProvider>
  )
}