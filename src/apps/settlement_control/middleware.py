"""验证每个请求的 Host，包含就绪探针。"""
def host_validation(get_response):
    def middleware(request):
        request.get_host()
        return get_response(request)
    return middleware
