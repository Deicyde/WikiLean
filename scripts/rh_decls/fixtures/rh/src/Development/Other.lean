module

namespace Development

theorem early : True := trivial

private theorem helper : True := trivial

public theorem shown : True := trivial

-- theorem commented : True := trivial

end Development

@[expose] public section

namespace Development

theorem late : True := trivial

end Development
