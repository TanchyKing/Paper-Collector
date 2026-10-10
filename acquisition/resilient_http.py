"""Reset a worker-local HTTP pool after a local connection acquisition timeout."""
import httpx

class RecoveringClient:
    def __init__(self,**kwargs):self.kwargs=kwargs;self.client=None;self.resets=0
    async def __aenter__(self):
        self.client=httpx.AsyncClient(**self.kwargs);await self.client.__aenter__();return self
    async def __aexit__(self,*args):return await self.client.__aexit__(*args)
    def stream(self,*args,**kwargs):return self.client.stream(*args,**kwargs)
    async def reset(self):
        await self.client.aclose();self.client=httpx.AsyncClient(**self.kwargs);await self.client.__aenter__();self.resets+=1
